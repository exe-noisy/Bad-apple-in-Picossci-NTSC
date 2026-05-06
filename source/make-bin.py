#!/usr/bin/env python3
"""
MP4 → 192x144 / 29.97fps / 16色パレット → .bin 変換スクリプト
+ 音声データ (PCM signed 16bit LE, インターリーブ L->R) を .bin 末尾に追記

出力バイナリ仕様:
  [映像部]
  - 解像度: 192 x 144 ピクセル
  - 1フレームあたりのバイト数: 192 * 144 / 2 = 13,824 bytes
  - 1バイトに 上位4bit=左(偶数列)ピクセル, 下位4bit=右(奇数列)ピクセルのパレット番号を格納
  - 全フレームを順番に連結

  [音声部 ─ 映像データの直後に追記]
  - 動画から音声を抽出し、PCM signed 16bit LE に変換
  - サンプル順: 左チャンネル(L)→右チャンネル(R) のインターリーブ
    例: [L0, R0, L1, R1, L2, R2, ...]
    各サンプルは 2バイト (signed 16bit little-endian)
  - ヘッダなし（生バイナリ）

使い方:
  python make-bin.py input.mp4 output.bin

オプション:
  --palette-image   パレット確認画像 (palette.png) を保存
  --preview-frame   最初のフレームのプレビュー画像 (preview.png) を保存
  --audio-rate INT  音声サンプリングレート変換先 (デフォルト: 元動画のレートを維持)
  --no-audio        音声を追記しない（映像のみ出力）
"""

import argparse
import subprocess
import sys
import os
import tempfile

import cv2
import numpy as np
from PIL import Image


# ─────────────────────────────────────────────
# 定数
# ─────────────────────────────────────────────
TARGET_W        = 192
TARGET_H        = 144
TARGET_FPS      = "30000/1001"          # ≈29.97fps (NTSC)
N_COLORS        = 16
BYTES_PER_FRAME = TARGET_W * TARGET_H // 2   # 13,824


# ─────────────────────────────────────────────
# ユーティリティ: ffmpeg 実行ラッパー
# ─────────────────────────────────────────────
def run_ffmpeg(cmd: list, label: str) -> None:
    """ffmpeg を実行し、失敗時はエラーを表示して終了する。"""
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        err_bytes = result.stderr[-2000:]
        for enc in ("utf-8", "cp932", "latin-1"):
            try:
                err_str = err_bytes.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            err_str = repr(err_bytes)
        print(f"{label} エラー:\n", err_str, file=sys.stderr)
        sys.exit(1)


# ─────────────────────────────────────────────
# ステップ1: ffmpeg 2パスでリサンプリング＋最適パレット生成
# ─────────────────────────────────────────────
def resample_video(input_path: str, tmp_path: str, palette_png: str) -> None:
    """
    パス1: palettegen で全フレームを統計解析して最適16色パレット画像を生成。
    パス2: paletteuse でそのパレットを使いディザリングしながら変換。
    """
    print("[1/5] ffmpeg パス1 – 最適パレットを解析中...")
    run_ffmpeg([
        "ffmpeg", "-y",
        "-i", input_path,
        "-vf", f"scale={TARGET_W}:{TARGET_H},palettegen=max_colors={N_COLORS}",
        palette_png
    ], "ffmpeg パス1")
    print(f"      → {palette_png}")

    print("[1/5] ffmpeg パス2 – パレット適用・変換中...")
    run_ffmpeg([
        "ffmpeg", "-y",
        "-i", input_path,
        "-i", palette_png,
        "-lavfi", f"fps={TARGET_FPS},scale={TARGET_W}:{TARGET_H}[x];[x][1:v]paletteuse=dither=bayer",
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-r", str(TARGET_FPS),
        "-an",
        tmp_path
    ], "ffmpeg パス2")
    print(f"      → {tmp_path}")


# ─────────────────────────────────────────────
# ステップ2: 全フレーム読み込み
# ─────────────────────────────────────────────
def load_frames(video_path: str) -> np.ndarray:
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        print(f"動画を開けません: {video_path}", file=sys.stderr)
        sys.exit(1)
    frames = []
    while True:
        ret, frame = cap.read()
        if not ret:
            break
        frames.append(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
    cap.release()
    arr = np.stack(frames, axis=0)
    print(f"[2/5] フレーム読み込み完了: {arr.shape[0]} フレーム ({TARGET_W}x{TARGET_H})")
    return arr


# ─────────────────────────────────────────────
# ステップ3: パレット画像からRGB値を読み取る
# ─────────────────────────────────────────────
def build_palette(palette_png: str) -> np.ndarray:
    """
    ffmpeg が生成したパレット画像から N_COLORS 個抽出して (16, 3) uint8 配列で返す。
    """
    print("[3/5] パレット画像から色情報を読み込み中...")
    img = Image.open(palette_png).convert("RGB")
    pixels = np.array(img, dtype=np.uint8).reshape(-1, 3)

    seen = []
    seen_set = set()
    for px in pixels:
        key = (int(px[0]), int(px[1]), int(px[2]))
        if key not in seen_set:
            seen_set.add(key)
            seen.append(px)
        if len(seen) >= N_COLORS:
            break

    while len(seen) < N_COLORS:
        seen.append(np.zeros(3, dtype=np.uint8))

    palette = np.stack(seen[:N_COLORS], axis=0).astype(np.uint8)

    print("      パレット (RGB):")
    for i, c in enumerate(palette):
        print(f"        [{i:2d}] #{c[0]:02X}{c[1]:02X}{c[2]:02X}  rgb({c[0]:3d},{c[1]:3d},{c[2]:3d})")

    return palette


# ─────────────────────────────────────────────
# ユーティリティ: 最近傍パレットインデックス取得
# ─────────────────────────────────────────────
def nearest_palette_index(pixels: np.ndarray, palette: np.ndarray) -> np.ndarray:
    diff  = pixels.astype(np.int16)[:, np.newaxis, :] \
          - palette.astype(np.int16)[np.newaxis, :, :]
    dist2 = (diff.astype(np.int32) ** 2).sum(axis=2)
    return dist2.argmin(axis=1).astype(np.uint8)


# ─────────────────────────────────────────────
# ステップ4: バイナリ変換・書き出し（映像部）
# ─────────────────────────────────────────────
def frames_to_binary(frames: np.ndarray, palette: np.ndarray, out_path: str) -> tuple[int, set]:
    """
    バイトフォーマット:
      bit7-4: 左ピクセル(偶数x)のパレット番号 (0-15)
      bit3-0: 右ピクセル(奇数x)のパレット番号 (0-15)
    """
    print("[4/5] フレームをバイナリに変換して書き出し中...")
    N = len(frames)
    total_bytes  = 0
    used_indices = set()

    with open(out_path, "wb") as f:
        for fi, frame in enumerate(frames):
            pixels  = frame.reshape(-1, 3)
            indices = nearest_palette_index(pixels, palette)

            used_indices.update(indices.tolist())

            evens  = indices[0::2]
            odds   = indices[1::2]
            packed = ((evens << 4) | odds).astype(np.uint8)

            f.write(packed.tobytes())
            total_bytes += len(packed)

            if (fi + 1) % 30 == 0 or fi == 0 or fi == N - 1:
                print(f"      フレーム {fi+1:5d} / {N} 完了")

    print(f"      合計 {N} フレーム / {total_bytes:,} bytes")
    return total_bytes, used_indices


# ─────────────────────────────────────────────
# ステップ5: 音声抽出・PCM変換・追記
# ─────────────────────────────────────────────
def append_audio(input_path: str, out_path: str) -> int:
    """
    動画から音声を抽出し、PCM signed 16bit LE (ステレオ) に変換して
    out_path の末尾に追記する。

    各サンプルは signed 16bit little-endian (2バイト)。

    戻り値: 追記した音声バイト数
    """
    print("[5/5] 音声を抽出・PCM 16bit LE 変換中 (R→L インターリーブ)...")

    # まず元動画に音声ストリームがあるか確認
    probe = subprocess.run(
        ["ffprobe", "-v", "error",
         "-select_streams", "a:0",
         "-show_entries", "stream=codec_type,sample_rate,channels",
         "-of", "default=noprint_wrappers=1",
         input_path],
        capture_output=True
    )
    probe_out = probe.stdout.decode("utf-8", errors="replace")
    if "codec_type=audio" not in probe_out:
        print("      ⚠️  音声ストリームが見つかりません。音声追記をスキップします。")
        return 0

    # サンプルレートは常に44100Hzに固定（入力ファイルのレートによらず）
    audio_rate = 44100
    print(f"      サンプルレート: {audio_rate} Hz (固定)")

    # チャンネル数確認（モノラルの場合は両チャンネルを同じにする）
    channels = 1
    for line in probe_out.splitlines():
        if line.startswith("channels="):
            try:
                channels = int(line.split("=")[1].strip())
            except ValueError:
                channels = 1
            break

    # ffmpeg で PCM raw バイナリを stdout に出力
    # s16le ステレオ出力は [L0, R0, L1, R1, ...] のサンプル単位インターリーブ
    cmd = [
        "ffmpeg", "-y",
        "-i", input_path,
        "-vn",                    # 映像を除外
        "-ar", str(audio_rate),   # サンプルレート (44100Hz固定)
        "-ac", "2",               # ステレオ出力
        "-f", "s16le",            # signed 16bit little-endian raw PCM
        "pipe:1"                  # stdout へ出力
    ]
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        err = result.stderr[-1000:].decode("utf-8", errors="replace")
        print(f"      ffmpeg 音声変換エラー:\n{err}", file=sys.stderr)
        sys.exit(1)

    # ffmpeg の出力はそのまま [L0, R0, L1, R1, ...] インターリーブなので追記するだけ
    audio_bytes = result.stdout
    n_samples = len(audio_bytes) // 4   # 2ch × 2bytes

    with open(out_path, "ab") as f:
        f.write(audio_bytes)

    print(f"      音声サンプル数  : {n_samples:,} サンプル/チャンネル")
    print(f"      音声データサイズ: {len(audio_bytes):,} bytes ({len(audio_bytes)/1024/1024:.2f} MB)")
    print(f"      チャンネル順    : L→R 交互インターリーブ [L0, R0, L1, R1, ...]")
    print(f"      フォーマット    : signed 16bit little-endian")
    return len(audio_bytes)


# ─────────────────────────────────────────────
# 使用色カラーコード出力
# ─────────────────────────────────────────────
def print_used_colors(palette: np.ndarray, used_indices: set) -> None:
    print()
    print("─" * 52)
    print(f"🎨  使用色一覧  ({len(used_indices)} / {N_COLORS} 色)")
    print("─" * 52)
    for i in sorted(used_indices):
        c = palette[i]
        r, g, b = int(c[0]), int(c[1]), int(c[2])
        print(f"  [{i:2d}]  #{r:02X}{g:02X}{b:02X}  rgb({r:3d}, {g:3d}, {b:3d})")

    unused = sorted(set(range(N_COLORS)) - used_indices)
    if unused:
        print()
        print(f"  （未使用パレット番号: {unused}）")
    print("─" * 52)


# ─────────────────────────────────────────────
# デバッグ用
# ─────────────────────────────────────────────
def save_preview_frame(frame: np.ndarray, palette: np.ndarray, out_path: str) -> None:
    pixels        = frame.reshape(-1, 3)
    indices       = nearest_palette_index(pixels, palette)
    reconstructed = palette[indices].reshape(TARGET_H, TARGET_W, 3).astype(np.uint8)
    Image.fromarray(reconstructed).save(out_path)
    print(f"      プレビュー画像 → {out_path}")


# ─────────────────────────────────────────────
# エントリーポイント
# ─────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="MP4 → 192x144 / 29.97fps / 16色パレット → .bin 変換ツール (音声付き)"
    )
    parser.add_argument("input",  help="入力 MP4 ファイルパス")
    parser.add_argument("output", help="出力 .bin ファイルパス")
    parser.add_argument("--palette-image", action="store_true",
                        help="パレット確認画像 (palette.png) を保存")
    parser.add_argument("--preview-frame", action="store_true",
                        help="先頭フレームのプレビュー (preview.png) を保存")
    parser.add_argument("--no-audio", action="store_true",
                        help="音声を追記しない（映像バイナリのみ出力）")
    args = parser.parse_args()

    if not os.path.exists(args.input):
        print(f"エラー: 入力ファイルが見つかりません: {args.input}", file=sys.stderr)
        sys.exit(1)

    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tf:
        tmp_video = tf.name
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf:
        tmp_palette_png = tf.name

    out_dir = os.path.dirname(os.path.abspath(args.output)) or "."

    try:
        # 1. 2パス変換（映像）
        resample_video(args.input, tmp_video, tmp_palette_png)

        # 2. フレーム読み込み
        frames = load_frames(tmp_video)

        # 3. パレット読み取り
        palette = build_palette(tmp_palette_png)

        # デバッグ画像（オプション）
        if args.palette_image:
            import shutil
            shutil.copy(tmp_palette_png, os.path.join(out_dir, "palette.png"))
            print(f"      パレット画像 → {os.path.join(out_dir, 'palette.png')}")
        if args.preview_frame and len(frames) > 0:
            save_preview_frame(frames[0], palette, os.path.join(out_dir, "preview.png"))

        # 4. 映像バイナリ書き出し
        video_bytes, used_indices = frames_to_binary(frames, palette, args.output)

        # 5. 音声を末尾に追記
        audio_bytes = 0
        if not args.no_audio:
            audio_bytes = append_audio(args.input, args.output)

        print_used_colors(palette, used_indices)

        total_bytes = video_bytes + audio_bytes
        print()
        print("=" * 60)
        print("✅  変換完了!")
        print(f"   入力ファイル        : {args.input}")
        print(f"   出力ファイル        : {args.output}")
        print()
        print("  [映像部]")
        print(f"   解像度             : {TARGET_W} x {TARGET_H}")
        print(f"   フレームレート      : ≈29.97 fps  (30000/1001)")
        print(f"   パレット           : ffmpeg palettegen（全フレーム統計）")
        print(f"   ディザリング        : Bayer scale=2")
        print(f"   パレット色数        : {N_COLORS} 色")
        print(f"   総フレーム数        : {len(frames):,}")
        print(f"   1フレームサイズ     : {BYTES_PER_FRAME:,} bytes")
        print(f"   映像データサイズ    : {video_bytes:,} bytes ({video_bytes/1024/1024:.2f} MB)")
        if not args.no_audio:
            print()
            print("  [音声部 ─ 映像末尾に追記]")
            print(f"   フォーマット        : signed 16bit little-endian PCM")
            print(f"   チャンネル順        : L→R 交互インターリーブ [L0, R0, L1, R1, ...]")
            print(f"   音声データサイズ    : {audio_bytes:,} bytes ({audio_bytes/1024/1024:.2f} MB)")
            print(f"   総セクター数        : {audio_bytes // 512:,} セクター (512 bytes/セクター)")
            print()
            print("  [バイナリレイアウト]")
            print(f"   0x00000000          映像データ開始")
            print(f"   0x{video_bytes:08X}          音声データ開始 (R→L インターリーブ PCM 16bit LE)")
            print(f"   0x{total_bytes:08X}          ファイル終端")
        print()
        print(f"   合計ファイルサイズ  : {total_bytes:,} bytes ({total_bytes/1024/1024:.2f} MB)")
        print("=" * 60)

    finally:
        for tmp in (tmp_video, tmp_palette_png):
            if os.path.exists(tmp):
                os.unlink(tmp)


if __name__ == "__main__":
    main()
