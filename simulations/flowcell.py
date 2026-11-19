"""Transient miscible displacement in a thin rectangular flow cell (SI internally)."""

from dataclasses import dataclass
from math import ceil, pi
from time import perf_counter

import numpy as np
from scipy import ndimage, sparse
from scipy.sparse.linalg import splu


@dataclass(frozen=True)
class Cell:
    length_mm: float = 56.2
    width_mm: float = 2.0
    gap_mm: float = 1.0
    needle_id_mm: float = 0.5
    needle_od_mm: float = 0.7
    needle_length_mm: float = 24.0
    inlet_depth_mm: float = 14.0
    outlet_depth_mm: float = 14.0
    inlet_offset_mm: float = 0.0
    outlet_offset_mm: float = 0.0
    beam_diameter_mm: float = 2.0
    beam_offset_x_mm: float = 0.0
    beam_offset_y_mm: float = 0.0

    @property
    def needle_volume_ml(self):
        return pi * (self.needle_id_mm / 2) ** 2 * self.needle_length_mm / 1000


@dataclass(frozen=True)
class Conditions:
    before_abv: float = 0.0
    after_abv: float = 10.0
    temperature_c: float = 20.0
    flow_ml_s: float = 0.34
    injected_ml: float = 1.5
    hold_s: float = 0.25
    orientation: str = "flat"
    diffusivity_m2_s: float = 1.0e-9


# Maximum step sizes in (gap z, width y, length x), in mm.
GRIDS = {
    "preview": (0.25, 0.40, 0.90),
    "standard": (0.167, 0.23, 0.48),
    "fine": (0.10, 0.12, 0.24),
}
GRAVITY = {
    "flat": (-9.80665, 0.0, 0.0),
    "inlet above": (0.0, 0.0, 9.80665),
    "inlet below": (0.0, 0.0, -9.80665),
}

# Khattab et al. (2012), Table 1, experimental columns, 293–308 K.
_MOLE = np.array([0, .033, .072, .117, .171, .236, .316, .419, .552, .735, 1])
_MASS = 46.068 * _MOLE / (46.068 * _MOLE + 18.015 * (1 - _MOLE))
_RHO = np.array([
    [998.7, 984.0, 973.7, 963.0, 947.3, 929.8, 908.6, 884.4, 858.9, 826.6, 791.0],
    [997.1, 982.3, 970.9, 957.7, 941.9, 923.2, 902.1, 878.8, 852.9, 826.6, 785.8],
    [994.0, 986.5, 968.1, 954.6, 941.2, 925.3, 905.1, 879.7, 852.9, 849.9, 781.3],
    [992.4, 978.5, 966.5, 954.6, 935.9, 916.8, 893.0, 870.7, 846.9, 815.2, 776.1],
])
_MU = 1e-3 * np.array([
    [1.0030, 1.2887, 1.8106, 2.3133, 2.6163, 3.0791, 3.0705, 2.7908, 2.3723, 1.6906, 1.1890],
    [.8914, 1.1703, 1.5963, 2.0140, 2.2875, 2.3869, 2.4236, 2.2249, 2.0271, 1.6594, 1.0995],
    [.7982, 1.0200, 1.4539, 1.7660, 1.7735, 1.8958, 2.0666, 1.8466, 1.7968, 1.5189, 1.0606],
    [.7202, .9015, 1.2355, 1.4731, 1.5412, 1.7346, 1.8349, 1.5690, 1.4408, 1.2371, 1.0112],
])
_ABV = 100 * _MASS * _RHO[0] / _RHO[0, -1]


def _at_temperature(table, temperature_c):
    """Interpolate adjacent rows. Extend the 20–25 °C slope down to 15 °C."""
    lower = min(2, max(0, int((temperature_c - 20) // 5)))
    weight = (temperature_c - 20 - 5 * lower) / 5
    return table[lower] + weight * (table[lower + 1] - table[lower])


def _slices(axis, start, stop):
    result = [slice(None)] * 3
    result[axis] = slice(start, stop)
    return tuple(result)


def _centers(face, axis):
    return (face[_slices(axis, 0, -1)] + face[_slices(axis, 1, None)]) * 0.5


def _faces(center, axis, xp):
    first = center[_slices(axis, 0, 1)]
    last = center[_slices(axis, -1, None)]
    return xp.concatenate((first, _centers(center, axis), last), axis=axis)


def _divergence(faces, steps, xp):
    return sum(xp.diff(face, axis=axis) / steps[axis] for axis, face in enumerate(faces))


def _disk_weights(y, z, dy, dz, radius, y_center, z_center, profile=False):
    """Subcell quadrature of a circular area or a parabolic aperture profile."""
    weights = np.zeros((len(z), len(y)))
    offsets = (np.arange(12) + 0.5) / 12 - 0.5
    for offset_y in offsets:
        for offset_z in offsets:
            r2 = ((y[None, :] + offset_y * dy - y_center) ** 2
                  + (z[:, None] + offset_z * dz - z_center) ** 2) / radius ** 2
            weights += np.maximum(0, 1 - r2) if profile else r2 < 1
    return weights / (len(offsets) ** 2)


def _mesh(cell, grid):
    dims = np.array((cell.gap_mm, cell.width_mm, cell.length_mm))
    depths = np.array((cell.inlet_depth_mm, cell.outlet_depth_mm))
    offsets = np.array((cell.inlet_offset_mm, cell.outlet_offset_mm))
    if not np.all(np.isfinite(dims)) or np.any(dims <= 0):
        raise ValueError("測定室の寸法には正の有限値を指定してください。")
    if not (0 < cell.needle_id_mm < cell.needle_od_mm < cell.gap_mm
            and 0 < cell.beam_diameter_mm <= min(cell.length_mm, cell.width_mm)
            and np.all(depths >= 0) and depths.sum() < cell.length_mm
            and np.all(depths <= cell.needle_length_mm)
            and np.all(abs(offsets) + cell.needle_od_mm / 2 < cell.width_mm / 2)):
        raise ValueError("針または照射域の寸法・位置を確認してください。")
    nz, ny, nx = (max(4, ceil(length / limit)) for length, limit in zip(dims, GRIDS[grid]))
    steps = dims * 1e-3 / (nz, ny, nx)
    dz, dy, dx = steps
    z, y, x = [(np.arange(n) + 0.5) * step
               for n, step in zip((nz, ny, nx), steps)]
    inlet = round(cell.inlet_depth_mm * 1e-3 / dx)
    outlet = nx - round(cell.outlet_depth_mm * 1e-3 / dx)
    if inlet >= outlet:
        raise ValueError("格子上で針先が重なります。格子を細かくするか挿入量を減らしてください。")

    fluid = np.ones((nz, ny, nx), dtype=bool)
    openings = []
    for index, shaft, offset in zip((inlet, outlet), (slice(None, inlet), slice(outlet, None)), offsets):
        center_y = dims[1] * 0.5e-3 + offset * 1e-3
        center_z = dims[0] * 0.5e-3
        aperture = _disk_weights(y, z, dy, dz, cell.needle_id_mm * 0.5e-3,
                                 center_y, center_z, profile=True)
        if aperture.sum() == 0:
            raise ValueError("開口を格子で解像できません。格子を細かくしてください。")
        radius2 = (y[None, :] - center_y) ** 2 + (z[:, None] - center_z) ** 2
        solid_shaft = (radius2 < (cell.needle_od_mm * 0.5e-3) ** 2) | (aperture > 0)
        fluid[:, :, shaft] &= ~solid_shaft[..., None]
        openings.append((index, aperture / (aperture.sum() * dy * dz)))

    if ndimage.label(fluid)[1] != 1:
        raise ValueError("測定室の液相が格子上で分断されています。格子を細かくしてください。")
    masks = []
    for axis in range(3):
        shape = list(fluid.shape)
        shape[axis] += 1
        mask = np.zeros(shape, dtype=bool)
        mask[_slices(axis, 1, -1)] = fluid[_slices(axis, 0, -1)] & fluid[_slices(axis, 1, None)]
        masks.append(mask)

    center_x = (cell.length_mm / 2 + cell.beam_offset_x_mm) * 1e-3
    center_y = (cell.width_mm / 2 + cell.beam_offset_y_mm) * 1e-3
    radius = cell.beam_diameter_mm * 0.5e-3
    if not (radius <= center_x <= dims[2] * 1e-3 - radius
            and radius <= center_y <= dims[1] * 1e-3 - radius):
        raise ValueError("照射域を測定室内に収めてください。")
    beam_yx = _disk_weights(x, y, dx, dy, radius, center_x, center_y)
    if np.any((beam_yx[None, :, :] > 0) & ~fluid):
        raise ValueError("照射域が格子上で針と重なっています。")

    return fluid, masks, steps, openings, beam_yx


def _laplacian_matrix(fluid, masks, steps):
    """Build -div(grad) on fluid cells, with zero normal pressure correction at walls."""
    ids = np.full(fluid.shape, -1)
    ids[fluid] = np.arange(fluid.sum())
    diagonal = np.zeros(fluid.sum())
    rows, cols, values = [], [], []
    for axis, mask in enumerate(masks):
        edges = mask[_slices(axis, 1, -1)]
        left = ids[_slices(axis, 0, -1)][edges]
        right = ids[_slices(axis, 1, None)][edges]
        weight = (1e-3 / steps[axis]) ** 2
        rows.extend((left, right))
        cols.extend((right, left))
        values.extend((np.full(left.size, -weight), np.full(right.size, -weight)))
        diagonal[left] += weight
        diagonal[right] += weight
    indices = np.arange(diagonal.size)
    rows.append(indices)
    cols.append(indices)
    values.append(diagonal)
    return sparse.csr_matrix(
        (np.concatenate(values), (np.concatenate(rows), np.concatenate(cols))),
        shape=(diagonal.size, diagonal.size),
    )


def _pressure_solver(matrix, fluid_host, steps, backend):
    if backend == "cpu":
        factor = splu(matrix[1:, 1:].tocsc())

        def solve(rhs):
            pressure = np.zeros_like(rhs)
            pressure[1:] = factor.solve(rhs[1:])
            return pressure

        return np, solve

    if backend != "cuda":
        raise ValueError("backend は 'cpu' または 'cuda' を指定してください。")
    try:
        import cupy as cp
        from cupyx.scipy.fft import dctn, idctn
        from cupyx.scipy.sparse import csr_matrix
        from cupyx.scipy.sparse.linalg import LinearOperator, cg
        cp.cuda.Device().use()
    except (ImportError, RuntimeError) as error:
        raise RuntimeError("CUDAバックエンドには、CUDA対応ドライバーとCuPyが必要です。") from error

    box_shape = fluid_host.shape
    fluid_indices = cp.asarray(np.flatnonzero(fluid_host))
    eigenvalues = cp.zeros(box_shape)
    for axis, (n, step) in enumerate(zip(box_shape, steps)):
        shape = [1, 1, 1]
        shape[axis] = n
        eigenvalues += (4 * (1e-3 / step) ** 2
                        * cp.sin(cp.pi * cp.arange(n) / (2 * n)) ** 2).reshape(shape)
    eigenvalues[0, 0, 0] = 1
    laplacian = csr_matrix(matrix)
    # Give the constant mode eigenvalue 1; other pressure modes are unchanged.
    operator = LinearOperator(matrix.shape, matvec=lambda v: laplacian @ v + v.mean(),
                              dtype=cp.float64)

    def precondition(vector):
        box = cp.zeros(box_shape, dtype=vector.dtype)
        box.ravel()[fluid_indices] = vector
        coefficients = dctn(box, norm="ortho") / eigenvalues
        coefficients[0, 0, 0] = 0
        projected = idctn(coefficients, norm="ortho").ravel()[fluid_indices]
        return projected - projected.mean() + vector.mean()

    preconditioner = LinearOperator(matrix.shape, matvec=precondition, dtype=cp.float64)
    previous = None

    def solve(rhs):
        nonlocal previous
        rhs = rhs - rhs.mean()
        pressure, status = cg(operator, rhs, x0=previous, M=preconditioner,
                              rtol=1e-8, atol=1e-14, maxiter=120)
        if status:
            raise RuntimeError(f"CUDA圧力の反復解法が収束しませんでした ({status})。")
        previous = pressure - pressure.mean()
        return previous

    return cp, solve


def _project(velocity, fluid, masks, steps, solve, xp):
    pressure = xp.zeros(fluid.shape)
    pressure[fluid] = solve(-1e-6 * _divergence(velocity, steps, xp)[fluid])
    for axis, face in enumerate(velocity):
        inner = _slices(axis, 1, -1)
        face[inner] -= masks[axis][inner] * xp.diff(pressure, axis=axis) / steps[axis]
    return velocity


def _momentum(velocity, masks, viscosity, acceleration, steps, dt, xp):
    """Explicit upwind momentum and local kinematic-viscosity Laplacian on MAC faces."""
    centered = [_centers(face, axis) for axis, face in enumerate(velocity)]
    updated = []
    for component, face in enumerate(velocity):
        nu_face = _faces(viscosity, component, xp)
        rate = _faces(acceleration[component], component, xp)
        for axis, step in enumerate(steps):
            backward = xp.roll(face, 1, axis=axis)
            forward = xp.roll(face, -1, axis=axis)
            low, high = _slices(axis, 0, 1), _slices(axis, -1, None)
            if component != axis:
                # Odd reflection imposes zero tangential speed on the glass/outer walls.
                backward[low] = -face[low]
                forward[high] = -face[high]
            advector = face if component == axis else _faces(centered[axis], component, xp)
            gradient = xp.where(advector >= 0, face - backward, forward - face) / step
            rate += nu_face * (forward - 2 * face + backward) / step ** 2 - advector * gradient
        updated.append(face + dt * masks[component] * rate)
    return updated


def _transport(tracer, velocity, masks, steps, diffusivity, inlet, dt, xp):
    """Conservative transport with minmod-limited face states and centered diffusion."""
    fluxes = []
    for axis, (face, step) in enumerate(zip(velocity, steps)):
        flux = xp.zeros_like(face)
        left_cells, right_cells = _slices(axis, 0, -1), _slices(axis, 1, None)
        inner = _slices(axis, 1, -1)
        left, right = tracer[left_cells], tracer[right_cells]
        delta = right - left
        open_face = masks[axis][inner]
        gradient = open_face * delta
        backward, forward = gradient[left_cells], gradient[right_cells]
        slope = xp.zeros_like(tracer)
        slope[inner] = xp.where(backward * forward > 0, xp.sign(backward)
                                * xp.minimum(xp.abs(backward), xp.abs(forward)), 0)
        advector = face[inner]
        flux[inner] = advector * xp.where(advector >= 0, left + 0.5 * slope[left_cells],
                                        right - 0.5 * slope[right_cells])
        flux[inner] -= diffusivity * gradient / step
        fluxes.append(flux)
    inlet_index, inlet_weights, inlet_value = inlet
    fluxes[2][:, :, inlet_index] = xp.where(
        inlet_weights > 0, velocity[2][:, :, inlet_index] * inlet_value, fluxes[2][:, :, inlet_index])
    # A face at the outer x boundary has no interior donor cell.
    fluxes[2][:, :, -1] = velocity[2][:, :, -1] * tracer[:, :, -1]
    return tracer - dt * _divergence(fluxes, steps, xp), fluxes[2]


def stable_window(times, values, threshold, hold_s):
    """Return the start and confirmation of the terminal qualifying sampled interval."""
    below = np.flatnonzero(values < threshold)
    index = below[-1] + 1 if below.size else 0
    if index == times.size:
        return None
    confirmed = np.searchsorted(times, times[index] + hold_s - 1e-12)
    return (times[index], times[confirmed]) if confirmed < times.size else None


def simulate(cell=Cell(), conditions=Conditions(), grid="preview", backend="cpu",
             frame_interval_s=0.05, progress=None, *, time_progress=None):
    """Return sampled responses and concentration fields during constant-flow injection.

    `progress` reports saved frame indices. `time_progress` reports simulated and total
    seconds at start, periodically while advancing, and at completion.
    """
    case = conditions
    if not (
        0 <= case.before_abv <= 100 and 0 <= case.after_abv <= 100
        and 15 <= case.temperature_c <= 35 and 0 < case.flow_ml_s < np.inf
        and 0 < case.injected_ml < np.inf and 0 <= case.hold_s < np.inf
        and 0 < frame_interval_s < np.inf and 0 < case.diffusivity_m2_s < np.inf
    ):
        raise ValueError("濃度、温度、流量、注入量、保持時間、拡散係数を確認してください。")
    start = perf_counter()
    simulation_s = case.injected_ml / case.flow_ml_s
    if time_progress is not None:
        time_progress(0.0, simulation_s)
    next_report = start + 1.0
    gravity = GRAVITY[case.orientation]
    fluid_host, mask_host, steps, ports, beam = _mesh(cell, grid)
    matrix = _laplacian_matrix(fluid_host, mask_host, steps)
    xp, solve = _pressure_solver(matrix, fluid_host, steps, backend)
    to_cpu = np.asarray if backend == "cpu" else xp.asnumpy
    fluid = xp.asarray(fluid_host)
    masks = [xp.asarray(mask) for mask in mask_host]
    beam = xp.asarray(beam)
    beam /= beam.sum() * fluid.shape[0]
    flow = case.flow_ml_s * 1e-6
    needle_delay_s = cell.needle_volume_ml / case.flow_ml_s
    frame_count = ceil(simulation_s / frame_interval_s)
    if frame_count * fluid_host.size > 55_000_000:
        raise ValueError("保存する濃度場が多すぎます。注入量を減らすか保存間隔を広げてください。")
    frames = np.linspace(0, simulation_s, frame_count + 1)
    (inlet_index, inlet_weights), (outlet_index, outlet_weights) = ports
    inlet_weights, outlet_weights = xp.asarray(inlet_weights), xp.asarray(outlet_weights)
    velocity = [xp.zeros(mask.shape) for mask in masks]
    for index, weights in ((inlet_index, inlet_weights), (outlet_index, outlet_weights)):
        plane = velocity[2][:, :, index]
        plane[weights > 0] = flow * weights[weights > 0]
    velocity = _project(velocity, fluid, masks, steps, solve, xp)
    tracer = xp.zeros(fluid.shape)
    mw_before, mw_after = np.interp([case.before_abv, case.after_abv], _ABV, _MASS)
    mass_axis = xp.asarray(_MASS)
    density_table = xp.asarray(_at_temperature(_RHO, case.temperature_c))
    viscosity_table = xp.asarray(_at_temperature(_MU, case.temperature_c))
    reference_density = float(np.interp(mw_before, _MASS, to_cpu(density_table)))
    max_nu = float((viscosity_table / reference_density).max().item())
    constant_rate = 2 * max(max_nu, case.diffusivity_m2_s) * (1 / steps ** 2).sum()
    cell_volume = steps.prod()
    face_area = steps[0] * steps[1]
    out_integral = 0.0
    t = 0.0
    inv_steps = xp.asarray(1 / steps)
    step_number = 0
    fields, optical, outlet = [], [], []

    for frame_index, target in enumerate(frames):
        while t < target - 1e-12:
            # Re-evaluate the CFL bound as the jet and buoyant circulation develop.
            if step_number % 8 == 0:
                rates = xp.stack([xp.abs(face).max() for face in velocity])
                max_dt = 0.36 / ((rates @ inv_steps).item() + constant_rate)
            boundary = min(target, needle_delay_s) if t < needle_delay_s - 1e-12 else target
            dt = min(max_dt, boundary - t)
            mw = mw_before + (mw_after - mw_before) * tracer
            density = xp.interp(mw, mass_axis, density_table)
            viscosity = xp.interp(mw, mass_axis, viscosity_table) / reference_density
            acceleration = [(density / reference_density - 1) * component for component in gravity]
            inlet_value = float(t >= needle_delay_s - 1e-12)
            tracer_new, flux = _transport(
                tracer, velocity, masks, steps, case.diffusivity_m2_s,
                (inlet_index, inlet_weights, inlet_value), dt, xp)
            tracer = xp.where(fluid, tracer_new, 0.0)
            out_integral += dt * (flux[:, :, outlet_index] * (outlet_weights > 0)).sum()
            velocity = _momentum(velocity, masks, viscosity, acceleration, steps, dt, xp)
            velocity = _project(velocity, fluid, masks, steps, solve, xp)
            t += dt
            step_number += 1
            if time_progress is not None and step_number % 8 == 0:
                now = perf_counter()
                if now >= next_report and t < simulation_s:
                    time_progress(t, simulation_s)
                    next_report = now + 1.0
        probe = tracer[:, :, outlet_index - 1]
        optical.append(float((tracer * beam).sum().item()))
        outlet.append(float((probe * outlet_weights).sum().item() * face_area))
        fields.append(to_cpu(xp.where(fluid, tracer, xp.nan)).astype(np.float32))
        frame = fields[-1]
        if not np.isfinite(frame[fluid_host]).all() or frame[fluid_host].min() < -1e-6 or frame[fluid_host].max() > 1.000001:
            raise RuntimeError("濃度場が発散しました。流量または格子条件を変更してください。")
        if progress is not None:
            progress(frame_index, frame_count)

    volume_scale = flow * simulation_s
    stored = float(tracer.sum().item()) * cell_volume
    escaped = float(out_integral) * face_area
    introduced = flow * max(0, simulation_s - needle_delay_s)
    balance = (stored + escaped - introduced) / volume_scale
    divergence = float(xp.abs(_divergence(velocity, steps, xp)[fluid]).max().item())
    if time_progress is not None:
        time_progress(simulation_s, simulation_s)
    return {
        "time_s": frames, "optical": np.asarray(optical), "outlet_tip": np.asarray(outlet),
        "fields": np.stack(fields), "cell": cell, "conditions": case,
        "grid": grid, "backend": backend, "needle_delay_s": needle_delay_s,
        "volume_ml": cell_volume * fluid_host.sum() * 1e6,
        "mass_balance_error": balance, "max_divergence_s_inv": divergence,
        "inlet_tip_x_mm": inlet_index * steps[2] * 1e3,
        "outlet_tip_x_mm": outlet_index * steps[2] * 1e3,
        "steps": step_number, "runtime_s": perf_counter() - start,
        "abv_mass_axis": _MASS.copy(), "abv_values": _ABV.copy(),
        "mass_endpoints": (mw_before, mw_after),
    }
