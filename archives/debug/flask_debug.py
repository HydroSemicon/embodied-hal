from flask import Flask, request # type: ignore

app = Flask(__name__)

@app.route("/command", methods=["POST"])
def handle_command():
    raw_bytes = request.data               # バイナリデータ
    text_data = raw_bytes.decode("utf-8", errors="replace")  # UTF-8として解釈
    print("データ受信:")
    print(repr(text_data))                 # 改行や不可視文字もわかるようにreprで表示
    return f"受信データ（repr）: {repr(text_data)}"

@app.route("/")
def home():
    return "Debug Flask server running (raw POST data viewer)"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
