#!/usr/bin/env python3
"""
clbs-youtube-edit / 全合成MP4レンダラー（ほぼ完成形）
ジェットカット連結 → スライド/ピクチャー/Bロール/ワイプ合成 → 見出しバー＋テロップ焼き込み。
入力: video.mp4 / jetcut_plan.json / timing_plan.json / telop_jetcut.json / 素材
出力: final.mp4 / heading_*.png / telop.ass / _filtergraph.txt
"""
from __future__ import annotations
import argparse
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
        # 英語フォールバック（en時はテロップ・見出しを描かないが、解決失敗でクラッシュさせない）
        "/System/Library/Fonts/Helvetica.ttc",                   # macOS Helvetica
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",     # macOS Arial Bold
        "/System/Library/Fonts/Supplemental/Arial.ttf",          # macOS Arial
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",  # Linux DejaVu
        "C:/Windows/Fonts/arialbd.ttf",                          # Windows Arial Bold
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


# ---------- 配色（ティール基調。参考フレーム準拠） ----------
TEAL = (41, 168, 201)          # ティール枠／テロップフチ #29A8C9
NAVY = (35, 59, 108)           # 濃紺文字 #233B6C
BOX_FILL = (248, 248, 246)     # 見出しボックス地（ほぼ白）


# ---------- 見出しバー PNG（白角丸＋ティール枠＋濃紺文字） ----------
def render_heading_png(text: str, out: Path) -> tuple[int, int]:
    # 既定=dark_gold（紺地×金枠×白字・ブッダ回デザイン＝Takumi標準。2026-07-06 既定反転）。
    # CLBS_HEADING_THEME=light で旧・白地×ティール枠×紺字
    theme = os.environ.get("CLBS_HEADING_THEME", "dark_gold")
    if theme == "dark_gold":
        box_fill = (14, 20, 38, 235)
        box_outline = (201, 166, 88, 255)
        text_fill = (255, 255, 255, 255)
    else:
        box_fill = (*BOX_FILL, 242)
        box_outline = (*TEAL, 255)
        text_fill = (*NAVY, 255)
    font = ImageFont.truetype(FONT, 46)
    pad_x, pad_y = 32, 16
    tmp = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    tb = tmp.textbbox((0, 0), text, font=font)
    tw, th = tb[2] - tb[0], tb[3] - tb[1]
    w, h = tw + pad_x * 2, th + pad_y * 2
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    rad = 18
    d.rounded_rectangle([1, 1, w - 2, h - 2], radius=rad, fill=box_fill,
                        outline=box_outline, width=3)
    d.text((pad_x - tb[0], pad_y - tb[1]), text, font=font, fill=text_fill)
    img.save(out)
    return w, h


# ---------- LINE案内バナー PNG（右上・全画面スライド中は非表示） ----------
def render_line_banner(out: Path) -> tuple[int, int]:
    f1 = ImageFont.truetype(FONT, 40)
    f2 = ImageFont.truetype(FONT, 27)
    GREEN = (6, 199, 85)
    YELLOW = (255, 214, 0)
    WHITE = (255, 255, 255)
    STROKE = (26, 26, 26)
    seg1 = [("LINE登録で", GREEN), ("【豪華3大特典】", YELLOW), ("を無料配布中", WHITE)]
    line2 = "特別動画をLINE限定公開中"
    tmp = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    w1 = sum(tmp.textlength(t, font=f1) for t, _ in seg1)
    w2 = tmp.textlength(line2, font=f2)
    sw = 3
    pad = 14
    gap = 6
    h1 = f1.getbbox("Ag")[3] + 6
    h2 = f2.getbbox("Ag")[3] + 4
    w = int(max(w1, w2) + pad * 2 + sw * 2)
    h = int(h1 + gap + h2 + pad * 2)
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    x = w - pad - int(w1) - sw
    y = pad
    for t, col in seg1:
        d.text((x, y), t, font=f1, fill=(*col, 255), stroke_width=sw, stroke_fill=(*STROKE, 255))
        x += int(tmp.textlength(t, font=f1))
    x2 = w - pad - int(w2) - sw
    d.text((x2, y + h1 + gap), line2, font=f2, fill=(*WHITE, 255),
           stroke_width=2, stroke_fill=(*STROKE, 255))
    img.save(out)
    return w, h


# ---------- テロップ ASS ----------
def ass_time(t: float) -> str:
    t = max(0.0, t)
    h = int(t // 3600); m = int((t % 3600) // 60); s = t % 60
    return f"{h:d}:{m:02d}:{s:05.2f}"


def build_ass(caps: list[dict], out: Path) -> None:
    # CLBS_TELOP_OUTLINE=RRGGBB でフチ色上書き（既定=紺233B6C・白文字紺縁＝Takumi標準。2026-07-06 既定反転）。
    # CLBS_TELOP_Y=920 等で中央下固定（an5\pos）。既定=下端アライン（従来挙動）。
    rgb = os.environ.get("CLBS_TELOP_OUTLINE", "233B6C").strip().lstrip("#")
    bgr = (rgb[4:6] + rgb[2:4] + rgb[0:2]).upper()
    y_env = os.environ.get("CLBS_TELOP_Y", "").strip()
    pos_tag = f"{{\\an5\\pos({W // 2},{int(y_env)})}}" if y_env else ""
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{FONT_NAME},90,&H00FFFFFF,&H00FFFFFF,&H00{bgr},&H64000000,-1,0,0,0,100,100,0,0,1,6,2,2,20,20,20,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = [head]
    for c in caps:
        txt = c["text"].replace("\n", "\\N")
        lines.append(f"Dialogue: 0,{ass_time(c['start'])},{ass_time(c['end'])},Default,,0,0,0,,{pos_tag}{txt}")
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
        return resolve(pdir, [f"スライド/slide_{num:03d}.png", f"slides/slide_{num:03d}.png", f"スライド/slide_{num:03d}.jpg",
                              f"slide{num:02d}.png", f"slide_{num:02d}.png", f"slide{num:02d}.jpg"])
    if typ == "broll":
        return resolve(pdir, [f"broll/broll{num:02d}.mp4", f"broll/broll{num}.mp4", f"Bロール/broll{num:02d}.mp4"])
    return None


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        print("usage: render_pro.py <project_dir> [--language ja|en]", file=sys.stderr); return 1
    ap = argparse.ArgumentParser()
    ap.add_argument("project_dir")
    ap.add_argument("--language", choices=["ja", "en"], default="ja",
                    help="en=英語動画モード（ジェットカットなし・テロップ/見出し焼き込みなし。合成レイアウトはjaと同一）")
    args = ap.parse_args(argv)
    lang = args.language
    pdir = Path(args.project_dir).expanduser().resolve()
    video = pdir / "video.mp4"
    jet = json.loads((pdir / "jetcut_plan.json").read_text(encoding="utf-8"))
    plan = json.loads((pdir / "timing_plan.json").read_text(encoding="utf-8"))
    caps = json.loads((pdir / "telop_jetcut.json").read_text(encoding="utf-8"))
    kept = jet["kept_chunks"]

    # 見出しPNG（en: 見出しバー焼き込みなし）
    headings = [e for e in plan["events"] if e["type"] == "heading"]
    hpng = []
    if lang != "en":
        for i, hd in enumerate(headings):
            p = pdir / f"heading_{i}.png"
            w, h = render_heading_png(hd["text"], p)
            hpng.append({"path": p, "w": w, "h": h, "intervals": hd.get("visible_intervals", [])})

    # LINE案内バナー（右上）: 既定=非表示（2026-07-06 既定反転。Takumi動画は右上LINE誘導禁止）。
    # 表示したい案件のみ CLBS_LINE_BANNER=1 でオプトイン（CLBS_NO_LINE_BANNER=1 は互換で常に抑止）
    banner = None
    if (lang != "en" and os.environ.get("CLBS_LINE_BANNER") == "1"
            and os.environ.get("CLBS_NO_LINE_BANNER") != "1"):
        bpath = pdir / "line_banner.png"
        bw, bh = render_line_banner(bpath)
        banner = {"path": bpath, "w": bw, "h": bh}

    # テロップASS（en: 焼き込みなし。SRT等のサイドカーは analyze 側の出力を維持）
    ass = pdir / "telop.ass"
    if lang != "en":
        build_ass(caps, ass)

    # 入力: 0=video, スライド各番号, ピクチャー各番号, broll(あれば), wipe, headings
    # 区間＋番号（イベントごとに対応する素材を割り当てる＝多スライド/多ピクチャー対応）
    def events_of(typ):
        out = []
        for e in plan["events"]:
            if e["type"] == typ and "jc_end" in e:
                out.append((e.get("number") or 1, e["jc_start"], e["jc_end"]))
        return out
    slide_events = events_of("slide")
    pic_events = events_of("picture")
    broll_events = events_of("broll")
    slide_iv = [(s, e) for _, s, e in slide_events]
    pic_iv = [(s, e) for _, s, e in pic_events]
    broll_iv = [(s, e) for _, s, e in broll_events]

    from collections import Counter
    slide_use = Counter(n for n, _, _ in slide_events)
    pic_use = Counter(n for n, _, _ in pic_events)
    slide_nums = sorted(slide_use)
    pic_nums = sorted(pic_use)
    slide_paths = {n: asset_for(pdir, "slide", n) for n in slide_nums}
    pic_paths = {n: asset_for(pdir, "picture", n) for n in pic_nums}
    for n in slide_nums:
        if not slide_paths[n]:
            print(f"[warn] missing slide asset #{n}", file=sys.stderr)
    for n in pic_nums:
        if not pic_paths[n]:
            print(f"[warn] missing picture asset #{n}", file=sys.stderr)
    broll = asset_for(pdir, "broll", 1) if broll_events else None
    # スライドが無い場合はワイプ不要（未接続[wipe]はffmpegエラーになる）
    use_wipe = bool(slide_iv and any(slide_paths.values()))

    inputs = ["-i", str(video)]
    idx = {"video": 0}
    nxt = 1
    idx_slide = {}
    for n in slide_nums:  # 画像はloop
        if slide_paths[n]:
            inputs += ["-loop", "1", "-i", str(slide_paths[n])]; idx_slide[n] = nxt; nxt += 1
    idx_pic = {}
    for n in pic_nums:
        if pic_paths[n]:
            inputs += ["-loop", "1", "-i", str(pic_paths[n])]; idx_pic[n] = nxt; nxt += 1
    if broll:  # 尺不足で静止しないよう無限ループ（表示はoverlay enableと-tで制限）
        inputs += ["-stream_loop", "-1", "-i", str(broll)]; idx["broll"] = nxt; nxt += 1
    if use_wipe:
        # ワイプ用 角丸マスク＋枠線
        wmask = pdir / "wipe_mask.png"; make_wipe_mask(wmask, WIPE_W, WIPE_H, WIPE_RADIUS)
        wborder = pdir / "wipe_border.png"; make_wipe_border(wborder, WIPE_W, WIPE_H, WIPE_RADIUS)
        inputs += ["-loop", "1", "-i", str(wmask)]; idx["wmask"] = nxt; nxt += 1
        inputs += ["-loop", "1", "-i", str(wborder)]; idx["wborder"] = nxt; nxt += 1
    for i, hp in enumerate(hpng):
        inputs += ["-loop", "1", "-i", str(hp["path"])]; idx[f"h{i}"] = nxt; nxt += 1
    if banner:
        inputs += ["-loop", "1", "-i", str(banner["path"])]; idx["banner"] = nxt; nxt += 1
        total_b = float(jet.get("total_jetcut", 0))
        fs = sorted(slide_iv + broll_iv)
        merged = []
        for s, e in fs:
            if merged and s <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], e))
            else:
                merged.append((s, e))
        biv = []; ct = 0.0
        for s, e in merged:
            if s > ct:
                biv.append((ct, s))
            ct = max(ct, e)
        if total_b > ct:
            biv.append((ct, total_b))
        banner["intervals"] = biv

    fc = []
    if lang == "en":
        # en: ジェットカットなし（元動画をそのまま基底に使用）
        fc.append("[0:v]split=2[base][wsrc]" if use_wipe else "[0:v]null[base]")
        fc.append("[0:a]anull[aout]")
    else:
        # 1) ジェットカット（select/aselectで一括: 入力を1回デコードして残す区間のみ抽出）
        keep_expr = "+".join(f"between(t,{esc(c['start'])},{esc(c['end'])})" for c in kept)
        fc.append(f"[0:v]select='{keep_expr}',setpts=N/FRAME_RATE/TB[basecat]")
        fc.append(f"[0:a]aselect='{keep_expr}',asetpts=N/SR/TB[aout]")
        fc.append("[basecat]split=2[base][wsrc]" if use_wipe else "[basecat]null[base]")
    if use_wipe:
        # ワイプ: 顔寄りクロップ→縮小→角丸マスク→枠線
        fc.append(f"[wsrc]crop={WIPE_CROP},scale={WIPE_W}:{WIPE_H},format=rgba[wcrop]")
        fc.append(f"[wcrop][{idx['wmask']}:v]alphamerge[wround]")
        fc.append(f"[wround][{idx['wborder']}:v]overlay=0:0[wipe]")

    # 2) 素材スケール（番号ごと。同一番号が複数回使われる場合は split で複製）
    for n in slide_nums:
        if n not in idx_slide:
            continue
        base = (f"[{idx_slide[n]}:v]scale={W}:{H}:force_original_aspect_ratio=decrease,"
                f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,setsar=1")
        k = slide_use[n]
        if k <= 1:
            fc.append(base + f"[sl{n}_0]")
        else:
            fc.append(base + f"[sl{n}base]")
            fc.append(f"[sl{n}base]split={k}" + "".join(f"[sl{n}_{j}]" for j in range(k)))
    for n in pic_nums:
        if n not in idx_pic:
            continue
        base = f"[{idx_pic[n]}:v]scale=-1:{PIC_H}"
        k = pic_use[n]
        if k <= 1:
            fc.append(base + f"[pc{n}_0]")
        else:
            fc.append(base + f"[pc{n}base]")
            fc.append(f"[pc{n}base]split={k}" + "".join(f"[pc{n}_{j}]" for j in range(k)))
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

    # スライド（全画面・区間ごとに対応スライド）→ ワイプ（右上・全スライド区間の和で1回）
    slide_take = {n: 0 for n in slide_nums}
    for n, s, e in slide_events:
        if n not in idx_slide:
            continue
        lbl = f"[sl{n}_{slide_take[n]}]"; slide_take[n] += 1
        cur = chain(cur, lbl, f"between(t,{esc(s)},{esc(e)})", "0:0")
    if use_wipe:
        expr = "+".join(f"between(t,{esc(s)},{esc(e)})" for _, s, e in slide_events)
        cur = chain(cur, "[wipe]", expr, f"{W-WIPE_W-40}:40")
    # ピクチャー（左寄せ・高さPIC_H・区間ごとに対応ピクチャー）
    pic_take = {n: 0 for n in pic_nums}
    for n, s, e in pic_events:
        if n not in idx_pic:
            continue
        lbl = f"[pc{n}_{pic_take[n]}]"; pic_take[n] += 1
        cur = chain(cur, lbl, f"between(t,{esc(s)},{esc(e)})", "60:(H-h)/2")
    # Bロール（全画面）
    if broll and broll_iv:
        expr = "+".join(f"between(t,{esc(s)},{esc(e)})" for s, e in broll_iv)
        cur = chain(cur, "[brollv]", expr, "0:0")
    # 見出しバー（左上・visible_intervalsのみ。en時は hpng が空）
    for i, hp in enumerate(hpng):
        if not hp["intervals"]:
            continue
        expr = "+".join(f"between(t,{esc(s)},{esc(e)})" for s, e in hp["intervals"])
        cur = chain(cur, f"[{idx[f'h{i}']}:v]", expr, "40:40")
    # LINE案内バナー（右上）
    if banner and banner.get("intervals"):
        expr = "+".join(f"between(t,{esc(s)},{esc(e)})" for s, e in banner["intervals"])
        cur = chain(cur, f"[{idx['banner']}:v]", expr, "W-w-30:24")
    if lang == "en":
        # en: テロップ焼き込みなし
        fc.append(f"{cur}null[vout]")
    else:
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
    vcodec = os.environ.get("CLBS_VCODEC", "libx264")
    if vcodec == "h264_videotoolbox":  # Apple Silicon HWエンコード（高速・ビットレート指定）
        venc = ["-c:v", "h264_videotoolbox", "-b:v", os.environ.get("CLBS_VBITRATE", "6000k")]
    else:
        venc = ["-c:v", "libx264", "-preset", preset, "-crf", crf]
    cmd = [ff(), "-y", *inputs, "-filter_complex_script", str(graph),
           "-map", "[vout]", "-map", "[aout]",
           "-r", str(FPS), *venc,
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
