#!/usr/bin/env python3
"""
takumi-youtube-edit / 全合成MP4レンダラー（clbs-youtube-edit render_pro.py の後継・日本語専用）

改善点（旧スキルからの差分）:
 1) 音声は asplit + atrim（サンプル精度）で各チャンク尺を「映像フレーム数÷fps」と厳密一致させる。
    映像 select は半開区間 [S, E)（0.5フレームシフト）で1チャンク=正確に(E-S)*fpsフレーム。
    → analyze の seg_bounds と実レンダが恒等になり、テロップ先行/音声遅れが構造的に消える。
 2) カット点に5msマイクロフェード（aselectハードカットのクリックノイズ除去）。
 3) 2パス loudnorm（-14 LUFS / TP -1.5、YouTube基準）。TAKUMI_LOUDNORM=0 で無効。
 4) 交互パンチイン（カットごとに100%/104%を交互適用）。TAKUMI_PUNCHIN=0 で無効。
 5) fps は ffprobe 実測（旧: 30固定。HeyGenの25fps素材で量子化誤差が拡大していた）。
 6) チョーク [ボードN] の左空間合成に対応（旧renderは未実装・XMLのみだった）。
 7) Bロールは番号ごとに個別入力（旧: broll01のみ全区間流用）。

入力: video.mp4 / jetcut_plan.json / timing_plan.json / telop_jetcut.json / 素材
出力: final.mp4 / heading_*.png / telop.ass / _filtergraph.txt
"""
from __future__ import annotations
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

W, H = 1920, 1080
AFADE = 0.005  # カット点マイクロフェード秒


def ff() -> str:
    env = os.environ.get("FFMPEG_PATH")
    if env and Path(env).exists():
        return env
    return shutil.which("ffmpeg") or "ffmpeg"


def _resolve_font() -> str:
    env = os.environ.get("CLBS_FONT")
    if env and Path(env).exists():
        return env
    candidates = [
        "/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc",
        "/System/Library/Fonts/ヒラギノ角ゴシック W7.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
        "C:/Windows/Fonts/YuGothB.ttc",
        "C:/Windows/Fonts/meiryob.ttc",
        "/System/Library/Fonts/Helvetica.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ]
    for c in candidates:
        if Path(c).exists():
            return c
    return candidates[0]


FONT = _resolve_font()
FONT_DIR = str(Path(FONT).parent)
FONT_NAME = os.environ.get("CLBS_FONT_NAME", "Hiragino Sans")
WIPE_W, WIPE_H = 252, 252
WIPE_RADIUS = 32
WIPE_CROP = os.environ.get("CLBS_WIPE_CROP", "760:760:910:170")
PIC_H = 608


def make_wipe_mask(out: Path, w: int, h: int, r: int) -> None:
    m = Image.new("L", (w, h), 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, w - 1, h - 1], radius=r, fill=255)
    m.save(out)


def make_wipe_border(out: Path, w: int, h: int, r: int) -> None:
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(img).rounded_rectangle([1, 1, w - 2, h - 2], radius=r,
                                          outline=(225, 235, 248, 220), width=4)
    img.save(out)


# ---------- 見出しバー PNG（Takumi既定: 紺地×金枠×白字。CLBS_HEADING_THEME=light で旧配色） ----------
def render_heading_png(text: str, out: Path) -> tuple[int, int]:
    theme = os.environ.get("CLBS_HEADING_THEME", "dark_gold")
    if theme == "dark_gold":
        box_fill = (14, 20, 38, 235)
        box_outline = (201, 166, 88, 255)
        text_fill = (255, 255, 255, 255)
    else:
        box_fill = (248, 248, 246, 242)
        box_outline = (41, 168, 201, 255)
        text_fill = (35, 59, 108, 255)
    font = ImageFont.truetype(FONT, 46)
    pad_x, pad_y = 32, 16
    tmp = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    tb = tmp.textbbox((0, 0), text, font=font)
    tw, th = tb[2] - tb[0], tb[3] - tb[1]
    w, h = tw + pad_x * 2, th + pad_y * 2
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([1, 1, w - 2, h - 2], radius=18, fill=box_fill,
                        outline=box_outline, width=3)
    d.text((pad_x - tb[0], pad_y - tb[1]), text, font=font, fill=text_fill)
    img.save(out)
    return w, h


# ---------- テロップ ASS ----------
def ass_time(t: float) -> str:
    t = max(0.0, t)
    h = int(t // 3600); m = int((t % 3600) // 60); s = t % 60
    return f"{h:d}:{m:02d}:{s:05.2f}"


def render_line_banner(out: Path) -> tuple[int, int]:
    """LINE登録バナー（CLBS_LINE_BANNER=1 でオプトイン・右上表示）"""
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


def build_ass(caps: list[dict], out: Path) -> None:
    # Takumi既定フチ=紺 #233B6C（レイアウト契約）。CLBS_TELOP_OUTLINE=RRGGBB で上書き
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
        return resolve(pdir, [f"ピクチャー/picture_{num:02d}.png", f"pictures/picture_{num:02d}.png",
                              f"ピクチャー/image{num:02d}.png", f"image{num:02d}.png"])
    if typ == "slide":
        return resolve(pdir, [f"スライド/slide_{num:03d}.png", f"slides/slide_{num:03d}.png",
                              f"スライド/slide_{num:03d}.jpg",
                              f"slide{num:02d}.png", f"slide_{num:02d}.png", f"slide{num:02d}.jpg"])
    if typ == "broll":
        return resolve(pdir, [f"broll/broll{num:02d}.mp4", f"broll/broll{num}.mp4",
                              f"Bロール/broll{num:02d}.mp4", f"broll{num:02d}.mp4"])
    if typ == "board":
        return resolve(pdir, [f"チョーク/Board{num}.png", f"チョーク/board_{num:02d}.png",
                              f"chalk/Board{num}.png", f"chalk/board_{num:02d}.png",
                              f"Board/board_{num:02d}.png", f"board_{num:02d}.png"])
    return None


# ---------- loudnorm 2パス ----------
def measure_loudnorm(ffmpeg: str, video: Path) -> str | None:
    """1パス目: 実測値を取り linear モードのフィルタ文字列を返す。失敗時 None。"""
    proc = subprocess.run(
        [ffmpeg, "-i", str(video), "-map", "a:0",
         "-af", "loudnorm=I=-14:TP=-1.5:LRA=11:print_format=json", "-f", "null", "-"],
        text=True, capture_output=True)
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", proc.stderr, re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
        return ("loudnorm=I=-14:TP=-1.5:LRA=11"
                f":measured_I={d['input_i']}:measured_TP={d['input_tp']}"
                f":measured_LRA={d['input_lra']}:measured_thresh={d['input_thresh']}"
                f":offset={d['target_offset']}:linear=true")
    except (ValueError, KeyError):
        return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("project_dir")
    ap.add_argument("--language", choices=["ja", "en"], default="ja",
                    help="en=英語動画モード（レガシーエンジンへ委譲）")
    args = ap.parse_args(argv)

    if args.language == "en":
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import render_pro_legacy
        return render_pro_legacy.main(argv)

    pdir = Path(args.project_dir).expanduser().resolve()
    video = pdir / "video.mp4"
    jet = json.loads((pdir / "jetcut_plan.json").read_text(encoding="utf-8"))
    plan = json.loads((pdir / "timing_plan.json").read_text(encoding="utf-8"))
    caps = json.loads((pdir / "telop_jetcut.json").read_text(encoding="utf-8"))
    kept = jet["kept_chunks"]
    fps = float(jet.get("fps") or plan.get("fps") or 30.0)
    hf = 0.5 / fps  # 半開区間シフト（境界フレームの二重取り/取りこぼし防止）
    total_jc = float(jet.get("total_jetcut", 0))

    seg_bounds = jet.get("seg_bounds")
    if not seg_bounds:
        seg_bounds = [0.0]
        acc = 0
        for c in kept:
            acc += int(round((c["end"] - c["start"]) * fps))
            seg_bounds.append(acc / fps)

    def esc(t: float) -> str:
        return f"{t:.6f}"

    def en_between(s: float, e: float) -> str:
        return f"between(t,{esc(s - hf)},{esc(e - hf)})"

    # 見出しPNG
    headings = [e for e in plan["events"] if e["type"] == "heading"]
    hpng = []
    for i, hd in enumerate(headings):
        p = pdir / f"heading_{i}.png"
        w, h = render_heading_png(hd["text"], p)
        hpng.append({"path": p, "w": w, "h": h, "intervals": hd.get("visible_intervals", [])})

    ass = pdir / "telop.ass"
    build_ass(caps, ass)

    banner = None
    if (os.environ.get("CLBS_LINE_BANNER") == "1"
            and os.environ.get("CLBS_NO_LINE_BANNER") != "1"):
        bpath = pdir / "line_banner.png"
        bw, bh = render_line_banner(bpath)
        banner = {"path": bpath, "w": bw, "h": bh}

    def events_of(typ):
        out = []
        for e in plan["events"]:
            if e["type"] == typ and "jc_end" in e:
                out.append((e.get("number") or 1, e["jc_start"], e["jc_end"]))
        return out

    slide_events = events_of("slide")
    pic_events = events_of("picture")
    broll_events = events_of("broll")
    board_events = events_of("board")
    slide_iv = [(s, e) for _, s, e in slide_events]

    from collections import Counter
    slide_use = Counter(n for n, _, _ in slide_events)
    pic_use = Counter(n for n, _, _ in pic_events)
    board_use = Counter(n for n, _, _ in board_events)
    slide_nums = sorted(slide_use)
    pic_nums = sorted(pic_use)
    board_nums = sorted(board_use)
    slide_paths = {n: asset_for(pdir, "slide", n) for n in slide_nums}
    pic_paths = {n: asset_for(pdir, "picture", n) for n in pic_nums}
    board_paths = {n: asset_for(pdir, "board", n) for n in board_nums}
    for label, nums, paths in (("slide", slide_nums, slide_paths),
                               ("picture", pic_nums, pic_paths),
                               ("board", board_nums, board_paths)):
        for n in nums:
            if not paths[n]:
                print(f"[warn] missing {label} asset #{n}", file=sys.stderr)
    broll_paths = []
    for k, (n, s, e) in enumerate(broll_events):
        p = asset_for(pdir, "broll", n)
        if not p:
            print(f"[warn] missing broll asset #{n}", file=sys.stderr)
        broll_paths.append(p)

    use_wipe = bool(slide_iv and any(slide_paths.values()))
    punchin = os.environ.get("TAKUMI_PUNCHIN", "1") != "0" and len(kept) > 1
    zoom = float(os.environ.get("TAKUMI_PUNCHIN_ZOOM", "1.04"))
    use_loudnorm = os.environ.get("TAKUMI_LOUDNORM", "1") != "0"

    inputs = ["-i", str(video)]
    idx: dict[str, int] = {"video": 0}
    nxt = 1
    idx_slide = {}
    for n in slide_nums:
        if slide_paths[n]:
            inputs += ["-loop", "1", "-i", str(slide_paths[n])]; idx_slide[n] = nxt; nxt += 1
    idx_pic = {}
    for n in pic_nums:
        if pic_paths[n]:
            inputs += ["-loop", "1", "-i", str(pic_paths[n])]; idx_pic[n] = nxt; nxt += 1
    idx_board = {}
    for n in board_nums:
        if board_paths[n]:
            inputs += ["-loop", "1", "-i", str(board_paths[n])]; idx_board[n] = nxt; nxt += 1
    idx_broll = []
    for k, p in enumerate(broll_paths):
        if p:
            inputs += ["-stream_loop", "-1", "-i", str(p)]; idx_broll.append(nxt); nxt += 1
        else:
            idx_broll.append(None)
    if use_wipe:
        wmask = pdir / "wipe_mask.png"; make_wipe_mask(wmask, WIPE_W, WIPE_H, WIPE_RADIUS)
        wborder = pdir / "wipe_border.png"; make_wipe_border(wborder, WIPE_W, WIPE_H, WIPE_RADIUS)
        inputs += ["-loop", "1", "-i", str(wmask)]; idx["wmask"] = nxt; nxt += 1
        inputs += ["-loop", "1", "-i", str(wborder)]; idx["wborder"] = nxt; nxt += 1
    for i, hp in enumerate(hpng):
        inputs += ["-loop", "1", "-i", str(hp["path"])]; idx[f"h{i}"] = nxt; nxt += 1
    if banner:
        inputs += ["-loop", "1", "-i", str(banner["path"])]; idx["banner"] = nxt; nxt += 1
        # 表示区間 = 全画面素材（スライド/Bロール）が出ていない時間帯
        _fs = sorted(slide_iv + [(s_, e_) for _, s_, e_ in broll_events])
        _mg = []
        for s_, e_ in _fs:
            if _mg and s_ <= _mg[-1][1]:
                _mg[-1] = (_mg[-1][0], max(_mg[-1][1], e_))
            else:
                _mg.append((s_, e_))
        _biv = []
        _ct = 0.0
        for s_, e_ in _mg:
            if s_ > _ct:
                _biv.append((_ct, s_))
            _ct = max(_ct, e_)
        if total_jc > _ct:
            _biv.append((_ct, total_jc))
        banner["intervals"] = _biv

    fc = []

    # ---- 映像ジェットカット（半開区間・1デコード）----
    keep_expr = "+".join(en_between(c["start"], c["end"]) for c in kept)
    fc.append(f"[0:v]select='{keep_expr}',setpts=N/FRAME_RATE/TB[jc]")
    splits = ["base0"]
    if punchin:
        splits.append("pzsrc")
    if use_wipe:
        splits.append("wsrc")
    if len(splits) == 1:
        fc.append("[jc]null[base0]")
    else:
        fc.append(f"[jc]split={len(splits)}" + "".join(f"[{s}]" for s in splits))

    # ---- 音声: asplit+atrim（サンプル精度）＋5msマイクロフェード ----
    n_chunks = len(kept)
    if n_chunks > 1:
        fc.append(f"[0:a]asplit={n_chunks}" + "".join(f"[ain{i}]" for i in range(n_chunks)))
        for i, c in enumerate(kept):
            frames = int(round((c["end"] - c["start"]) * fps))
            d = frames / fps  # 映像フレーム数と厳密一致させる
            end = c["start"] + d
            fo = max(d - AFADE, 0.0)
            fc.append(f"[ain{i}]atrim=start={esc(c['start'])}:end={esc(end)},asetpts=PTS-STARTPTS,"
                      f"afade=t=in:st=0:d={AFADE},afade=t=out:st={esc(fo)}:d={AFADE}[ac{i}]")
        fc.append("".join(f"[ac{i}]" for i in range(n_chunks)) + f"concat=n={n_chunks}:v=0:a=1[acat]")
    else:
        fc.append("[0:a]anull[acat]")
    ln = measure_loudnorm(ff(), video) if use_loudnorm else None
    if use_loudnorm and not ln:
        print("[warn] loudnorm measurement failed; skipping normalization", file=sys.stderr)
    if ln:
        fc.append(f"[acat]{ln},aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo[aout]")
    else:
        fc.append("[acat]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo[aout]")

    cur = "[base0]"; step = 0

    def chain(src, ov, expr, pos):
        nonlocal step
        out = f"[c{step}]"; step += 1
        fc.append(f"{src}{ov}overlay={pos}:enable='{expr}'{out}")
        return out

    # ---- 交互パンチイン（奇数セグメントに104%）----
    if punchin:
        zw = f"trunc(iw*{zoom}/2)*2"
        zh = f"trunc(ih*{zoom}/2)*2"
        fc.append(f"[pzsrc]scale={zw}:{zh},crop={W}:{H}[pz]")
        odd_iv = [(seg_bounds[i], seg_bounds[i + 1])
                  for i in range(1, len(seg_bounds) - 1, 2)]
        if odd_iv:
            expr = "+".join(en_between(s, e) for s, e in odd_iv)
            cur = chain(cur, "[pz]", expr, "0:0")

    # ---- ワイプ ----
    if use_wipe:
        fc.append(f"[wsrc]crop={WIPE_CROP},scale={WIPE_W}:{WIPE_H},format=rgba[wcrop]")
        fc.append(f"[wcrop][{idx['wmask']}:v]alphamerge[wround]")
        fc.append(f"[wround][{idx['wborder']}:v]overlay=0:0[wipe]")

    # ---- 素材スケール ----
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
    board_scale = float(os.environ.get("TAKUMI_BOARD_SCALE", "0.52"))
    for n in board_nums:
        if n not in idx_board:
            continue
        base = f"[{idx_board[n]}:v]scale=trunc(iw*{board_scale}/2)*2:-2"
        k = board_use[n]
        if k <= 1:
            fc.append(base + f"[bd{n}_0]")
        else:
            fc.append(base + f"[bd{n}base]")
            fc.append(f"[bd{n}base]split={k}" + "".join(f"[bd{n}_{j}]" for j in range(k)))
    for k, (bi, (n, s, e)) in enumerate(zip(idx_broll, broll_events)):
        if bi is None:
            continue
        fc.append(f"[{bi}:v]scale={W}:{H}:force_original_aspect_ratio=decrease,"
                  f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,setsar=1,"
                  f"setpts=PTS-STARTPTS+{esc(s)}/TB[br{k}]")

    # ---- 合成（スライド→ワイプ→ピクチャー→ボード→Bロール→見出し）----
    slide_take = {n: 0 for n in slide_nums}
    for n, s, e in slide_events:
        if n not in idx_slide:
            continue
        lbl = f"[sl{n}_{slide_take[n]}]"; slide_take[n] += 1
        cur = chain(cur, lbl, en_between(s, e), "0:0")
    if use_wipe:
        expr = "+".join(en_between(s, e) for _, s, e in slide_events)
        cur = chain(cur, "[wipe]", expr, f"{W-WIPE_W-40}:40")
    pic_take = {n: 0 for n in pic_nums}
    for n, s, e in pic_events:
        if n not in idx_pic:
            continue
        lbl = f"[pc{n}_{pic_take[n]}]"; pic_take[n] += 1
        cur = chain(cur, lbl, en_between(s, e), "60:(H-h)/2")
    board_take = {n: 0 for n in board_nums}
    bx = int(os.environ.get("TAKUMI_BOARD_X", "490"))
    by = int(os.environ.get("TAKUMI_BOARD_Y", "540"))
    for n, s, e in board_events:
        if n not in idx_board:
            continue
        lbl = f"[bd{n}_{board_take[n]}]"; board_take[n] += 1
        cur = chain(cur, lbl, en_between(s, e), f"{bx}-overlay_w/2:{by}-overlay_h/2")
    for k, (bi, (n, s, e)) in enumerate(zip(idx_broll, broll_events)):
        if bi is None:
            continue
        cur = chain(cur, f"[br{k}]", en_between(s, e), "0:0")
    for i, hp in enumerate(hpng):
        if not hp["intervals"]:
            continue
        expr = "+".join(en_between(s, e) for s, e in hp["intervals"])
        cur = chain(cur, f"[{idx[f'h{i}']}:v]", expr, "40:40")

    # ---- LINEバナー（右上・全画面素材中は非表示）----
    if banner and banner.get("intervals"):
        expr = "+".join(en_between(s_, e_) for s_, e_ in banner["intervals"])
        cur = chain(cur, f"[{idx['banner']}:v]", expr, "W-w-30:24")

    # ---- テロップ焼き込み ----
    ass_path = str(ass).replace("\\", "/").replace(":", r"\:")
    fontsdir = FONT_DIR.replace("\\", "/").replace(":", r"\:")
    fc.append(f"{cur}subtitles='{ass_path}':fontsdir='{fontsdir}'[vout]")

    graph = pdir / "_filtergraph.txt"
    graph.write_text(";\n".join(fc), encoding="utf-8")

    out = pdir / "final.mp4"
    preset = os.environ.get("CLBS_PRESET", "veryfast")
    crf = os.environ.get("CLBS_CRF", "21")
    vcodec = os.environ.get("CLBS_VCODEC", "libx264")
    if vcodec == "h264_videotoolbox":
        venc = ["-c:v", "h264_videotoolbox", "-b:v", os.environ.get("CLBS_VBITRATE", "6000k")]
    else:
        venc = ["-c:v", "libx264", "-preset", preset, "-crf", crf]
    r_out = str(int(round(fps))) if abs(fps - round(fps)) < 0.01 else f"{fps:.5f}"
    cmd = [ff(), "-y", *inputs, "-filter_complex_script", str(graph),
           "-map", "[vout]", "-map", "[aout]",
           "-r", r_out, *venc,
           "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k"]
    if total_jc:
        cmd += ["-t", f"{total_jc:.6f}"]
    cmd += [str(out)]
    print(f"[render] fps={fps:g} chunks={len(kept)} punchin={'on' if punchin else 'off'} "
          f"loudnorm={'on' if ln else 'off'} headings={len(hpng)} caps={len(caps)}")
    proc = subprocess.run(cmd, text=True, capture_output=True)
    if proc.returncode != 0:
        print("FFMPEG ERROR (tail):", file=sys.stderr)
        print("\n".join(proc.stderr.splitlines()[-25:]), file=sys.stderr)
        return 1
    print(f"- {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
