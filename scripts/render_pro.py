#!/usr/bin/env python3
"""
clbs-video-edit-pro / 全合成MP4レンダラー（ほぼ完成形）
ジェットカット連結 → スライド/ピクチャー/Bロール/ワイプ合成 → 見出しバー＋テロップ焼き込み。
入力: video.mp4 / jetcut_plan.json / timing_plan.json / telop_jetcut.json / 素材
出力: final.mp4 / heading_*.png / telop.ass / _filtergraph.txt
"""
from __future__ import annotations
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

import shutil

FPS = 30
W, H = 1920, 1080


def _resolve_font() -> str:
    """太ゴシック日本語フォントを解決（環境変数 > macOS > Linux Noto）。"""
    env = os.environ.get("CLBS_FONT")
    if env and Path(env).exists():
        return env
    candidates = [
        "/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc",       # macOS
        "/System/Library/Fonts/ヒラギノ角ゴシック W7.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",   # Linux (Noto)
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
        "C:/Windows/Fonts/YuGothB.ttc",                          # Windows 游ゴシック Bold
        "C:/Windows/Fonts/meiryob.ttc",                          # Windows メイリオ Bold
    ]
    for c in candidates:
        if Path(c).exists():
            return c
    return candidates[0]  # フォールバック（存在しなくてもPILがエラーを出す）


FONT = _resolve_font()
FONT_DIR = str(Path(FONT).parent)
FONT_NAME = os.environ.get("CLBS_FONT_NAME", "Hiragino Sans")  # libass 解決用ファミリ名
WIPE_W, WIPE_H = 252, 252             # 正方形PiP（角丸）。一辺=旧360の70%
WIPE_RADIUS = 32                      # 角丸の半径
# ワイプは人物の顔＋肩が入る正方形領域を切り抜いてから縮小。
# crop=w:h:x:y（元1920x1080・話者は中央右）。環境変数 CLBS_WIPE_CROP="w:h:x:y" で上書き可
WIPE_CROP = os.environ.get("CLBS_WIPE_CROP", "760:760:910:170")
PIC_H = 608                           # ピクチャー高さ（左寄せ）。旧760の80%


def make_wipe_mask(out: Path, w: int, h: int, r: int) -> None:
    m = Image.new("L", (w, h), 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, w - 1, h - 1], radius=r, fill=255)
    m.save(out)


def make_wipe_border(out: Path, w: int, h: int, r: int) -> None:
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(img).rounded_rectangle([1, 1, w - 2, h - 2], radius=r,
                                          outline=(225, 235, 248, 220), width=4)
    img.save(out)


def ff() -> str:
    env = os.environ.get("FFMPEG_PATH")
    if env and Path(env).exists():
        return env
    return shutil.which("ffmpeg") or "ffmpeg"


def esc(t: float) -> str:
    return f"{t:.3f}"


# ---------- 見出しバー PNG ----------
def render_heading_png(text: str, out: Path) -> tuple[int, int]:
    font = ImageFont.truetype(FONT, 46)
    pad_x, pad_y = 34, 16
    tmp = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    tb = tmp.textbbox((0, 0), text, font=font)
    tw, th = tb[2] - tb[0], tb[3] - tb[1]
    w, h = tw + pad_x * 2, th + pad_y * 2
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # 紺→黒グラデ
    for y in range(h):
        r = int(14 + (1 - y / h) * 16); g = int(20 + (1 - y / h) * 26); b = int(48 + (1 - y / h) * 50)
        d.line([(0, y), (w, y)], fill=(r, g, b, 235))
    # 星屑（決定的）
    seed = 12345
    for i in range(40):
        seed = (seed * 1103515245 + 12345) & 0x7fffffff
        sx = seed % w; seed = (seed * 1103515245 + 12345) & 0x7fffffff; sy = seed % h
        d.point((sx, sy), fill=(200, 220, 255, 160))
    # ゴールド枠
    d.rectangle([1, 1, w - 2, h - 2], outline=(212, 175, 80, 255), width=3)
    # 白太文字
    d.text((pad_x - tb[0], pad_y - tb[1]), text, font=font, fill=(255, 255, 255, 255))
    img.save(out)
    return w, h


# ---------- テロップ ASS ----------
def ass_time(t: float) -> str:
    t = max(0.0, t)
    h = int(t // 3600); m = int((t % 3600) // 60); s = t % 60
    return f"{h:d}:{m:02d}:{s:05.2f}"


def build_ass(caps: list[dict], out: Path) -> None:
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{FONT_NAME},90,&H00FFFFFF,&H00FFFFFF,&H006C3B23,&H64000000,-1,0,0,0,100,100,0,0,1,6,2,2,20,20,20,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = [head]
    for c in caps:
        txt = c["text"].replace("\n", "\\N")
        lines.append(f"Dialogue: 0,{ass_time(c['start'])},{ass_time(c['end'])},Default,,0,0,0,,{txt}")
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------- 素材解決 ----------
def resolve(pdir: Path, names: list[str]) -> Path | None:
    for n in names:
        p = pdir / n
        if p.exists():
            return p
    return None


def asset_for(pdir: Path, typ: str, num: int) -> Path | None:
    if typ == "picture":
        return resolve(pdir, [f"ピクチャー/picture_{num:02d}.png", f"pictures/picture_{num:02d}.png", f"image{num:02d}.png"])
    if typ == "slide":
        return resolve(pdir, [f"スライド/slide_{num:03d}.png", f"slides/slide_{num:03d}.png", f"スライド/slide_{num:03d}.jpg"])
    if typ == "broll":
        return resolve(pdir, [f"broll/broll{num:02d}.mp4", f"broll/broll{num}.mp4", f"Bロール/broll{num:02d}.mp4"])
    return None


def main(argv=None) -> int:
    argv = argv or sys.argv[1:]
    if not argv:
        print("usage: render_pro.py <project_dir>", file=sys.stderr); return 1
    pdir = Path(argv[0]).expanduser().resolve()
    video = pdir / "video.mp4"
    jet = json.loads((pdir / "jetcut_plan.json").read_text(encoding="utf-8"))
    plan = json.loads((pdir / "timing_plan.json").read_text(encoding="utf-8"))
    caps = json.loads((pdir / "telop_jetcut.json").read_text(encoding="utf-8"))
    kept = jet["kept_chunks"]

    # 見出しPNG
    headings = [e for e in plan["events"] if e["type"] == "heading"]
    hpng = []
    for i, hd in enumerate(headings):
        p = pdir / f"heading_{i}.png"
        w, h = render_heading_png(hd["text"], p)
        hpng.append({"path": p, "w": w, "h": h, "intervals": hd.get("visible_intervals", [])})

    # テロップASS
    ass = pdir / "telop.ass"
    build_ass(caps, ass)

    # 入力: 0=video, 1=slide,2=picture,3=broll(あれば), then headings
    slide = asset_for(pdir, "slide", 1)
    pic = asset_for(pdir, "picture", 1)
    broll = asset_for(pdir, "broll", 1)
    inputs = ["-i", str(video)]
    idx = {"video": 0}
    nxt = 1
    for key, path in (("slide", slide), ("picture", pic)):  # 画像はloop
        if path:
            inputs += ["-loop", "1", "-i", str(path)]; idx[key] = nxt; nxt += 1
    if broll:  # 尺不足で静止しないよう無限ループ（表示はoverlay enableと-tで制限）
        inputs += ["-stream_loop", "-1", "-i", str(broll)]; idx["broll"] = nxt; nxt += 1
    # ワイプ用 角丸マスク＋枠線
    wmask = pdir / "wipe_mask.png"; make_wipe_mask(wmask, WIPE_W, WIPE_H, WIPE_RADIUS)
    wborder = pdir / "wipe_border.png"; make_wipe_border(wborder, WIPE_W, WIPE_H, WIPE_RADIUS)
    inputs += ["-loop", "1", "-i", str(wmask)]; idx["wmask"] = nxt; nxt += 1
    inputs += ["-loop", "1", "-i", str(wborder)]; idx["wborder"] = nxt; nxt += 1
    for i, hp in enumerate(hpng):
        inputs += ["-loop", "1", "-i", str(hp["path"])]; idx[f"h{i}"] = nxt; nxt += 1

    fc = []
    # 1) ジェットカット（select/aselectで一括: 入力を1回デコードして残す区間のみ抽出）
    keep_expr = "+".join(f"between(t,{esc(c['start'])},{esc(c['end'])})" for c in kept)
    fc.append(f"[0:v]select='{keep_expr}',setpts=N/FRAME_RATE/TB[basecat]")
    fc.append(f"[0:a]aselect='{keep_expr}',asetpts=N/SR/TB[aout]")
    fc.append("[basecat]split=2[base][wsrc]")
    # ワイプ: 顔寄りクロップ→縮小→角丸マスク→枠線
    fc.append(f"[wsrc]crop={WIPE_CROP},scale={WIPE_W}:{WIPE_H},format=rgba[wcrop]")
    fc.append(f"[wcrop][{idx['wmask']}:v]alphamerge[wround]")
    fc.append(f"[wround][{idx['wborder']}:v]overlay=0:0[wipe]")

    # 区間
    def intervals(typ):
        return [(e["jc_start"], e["jc_end"]) for e in plan["events"]
                if e["type"] == typ and "jc_end" in e]
    slide_iv = intervals("slide"); pic_iv = intervals("picture"); broll_iv = intervals("broll")

    # 2) 素材スケール
    if slide:
        fc.append(f"[{idx['slide']}:v]scale={W}:{H}:force_original_aspect_ratio=decrease,"
                  f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,setsar=1[slidev]")
    if pic:
        fc.append(f"[{idx['picture']}:v]scale=-1:{PIC_H}[picv]")
    if broll:
        # 動画はPTSを表示区間開始へオフセット（画像と違い実時間で流れるため）
        b_off = broll_iv[0][0] if broll_iv else 0.0
        fc.append(f"[{idx['broll']}:v]scale={W}:{H}:force_original_aspect_ratio=decrease,"
                  f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,setsar=1,"
                  f"setpts=PTS-STARTPTS+{esc(b_off)}/TB[brollv]")

    cur = "[base]"; step = 0

    def chain(src, ov, expr, pos):
        nonlocal step
        out = f"[c{step}]"; step += 1
        fc.append(f"{src}{ov}overlay={pos}:enable='{expr}'{out}")
        return out

    # スライド（全画面）→ ワイプ（右上）
    if slide and slide_iv:
        expr = "+".join(f"between(t,{esc(s)},{esc(e)})" for s, e in slide_iv)
        cur = chain(cur, "[slidev]", expr, "0:0")
        cur = chain(cur, "[wipe]", expr, f"{W-WIPE_W-40}:40")
    # ピクチャー（左）
    if pic and pic_iv:
        expr = "+".join(f"between(t,{esc(s)},{esc(e)})" for s, e in pic_iv)
        cur = chain(cur, "[picv]", expr, f"60:(H-h)/2")
    # Bロール（全画面）
    if broll and broll_iv:
        expr = "+".join(f"between(t,{esc(s)},{esc(e)})" for s, e in broll_iv)
        cur = chain(cur, "[brollv]", expr, "0:0")
    # 見出しバー（左上・visible_intervalsのみ）
    for i, hp in enumerate(hpng):
        if not hp["intervals"]:
            continue
        expr = "+".join(f"between(t,{esc(s)},{esc(e)})" for s, e in hp["intervals"])
        cur = chain(cur, f"[{idx[f'h{i}']}:v]", expr, "40:40")
    # テロップ焼き込み
    ass_path = str(ass).replace("\\", "/").replace(":", r"\:")
    fontsdir = FONT_DIR.replace("\\", "/").replace(":", r"\:")
    fc.append(f"{cur}subtitles='{ass_path}':fontsdir='{fontsdir}'[vout]")

    graph = pdir / "_filtergraph.txt"
    graph.write_text(";\n".join(fc), encoding="utf-8")

    out = pdir / "final.mp4"
    total_jc = float(jet.get("total_jetcut", 0)) or None
    preset = os.environ.get("CLBS_PRESET", "veryfast")
    crf = os.environ.get("CLBS_CRF", "21")
    cmd = [ff(), "-y", *inputs, "-filter_complex_script", str(graph),
           "-map", "[vout]", "-map", "[aout]",
           "-r", str(FPS), "-c:v", "libx264", "-preset", preset, "-crf", crf,
           "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k"]
    if total_jc:
        cmd += ["-t", f"{total_jc:.3f}"]   # 尺を安全にキャップ
    cmd += [str(out)]
    print(f"[render] inputs={len(inputs)//2} kept={len(kept)} headings={len(hpng)} caps={len(caps)}")
    proc = subprocess.run(cmd, text=True, capture_output=True)
    if proc.returncode != 0:
        print("FFMPEG ERROR (tail):", file=sys.stderr)
        print("\n".join(proc.stderr.splitlines()[-25:]), file=sys.stderr)
        return 1
    print(f"- {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
