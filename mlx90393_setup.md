OS dependencies:
sudo apt install python3-tk python3-pil.imagetk

Python environment:
python3 -m venv --system-site-packages ~/venvs/eflesh
source ~/venvs/eflesh/bin/activate
pip install -r requirements.txt

Raspberry Pi:
I2C enabled
MLX90393 address = 0x18