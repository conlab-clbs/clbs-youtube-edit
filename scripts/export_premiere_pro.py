#!/usr/bin/env python3
"""
clbs-youtube-edit / Premiere XML 書き出し（XMEML v4）
入力: jetcut_plan.json + timing_plan.json + video.mp4 + 素材
トラック契約（厳守）:
  V1 メイン動画（ジェットカット済み）/ V2 ピクチャー / V3 スライド /
  V4 Bロール / V5 ワイプ（スライド中のみ・右上）/ A1 左ch / A2 右ch
※ 見出し・テロップは焼き込み(render_pro.py)とSRT(telop_jetcut.srt)で対応。
"""
from __future__ import annotations
import html
import json
import sys
from pathlib import Path
from urllib.parse import quote

FPS = 30
W, H = 1920, 1080

# 手仕上げ用のモーション既定値（インポート時は無視されるため目安）
PICTURE_POS = (560, 540); PICTURE_SCALE = 62      # 左寄せ
SLIDE_POS = (960, 540); SLIDE_SCALE = 100         # 中央フル
WIPE_POS = (1632, 216); WIPE_SCALE = 25           # 右上（見本準拠）


def pathurl(p: Path) -> str:
    return "file://localhost" + quote(str(p.resolve()))


def resolve(pdir: Path, names: list[str]) -> Path | None:
    for n in names:
        p = pdir / n
        if p.exists():
            return p
    return None


def asset_for(pdir: Path, typ: str, num: int) -> Path | None:
    if typ == "picture":
        return resolve(pdir, [f"ピクチャー/picture_{num:02d}.png", f"pictures/picture_{num:02d}.png",
                              f"ピクチャー/image{num:02d}.png", f"image{num:02d}.png", f"image{num}.png"])
    if typ == "slide":
        return resolve(pdir, [f"スライド/slide_{num:03d}.png", f"slides/slide_{num:03d}.png",
                              f"スライド/slide_{num:03d}.jpg", f"slides_page-{num}.jpg"])
    if typ == "broll":
        return resolve(pdir, [f"broll/broll{num:02d}.mp4", f"broll/broll{num}.mp4",
                              f"Bロール/broll{num:02d}.mp4", f"broll{num:02d}.mp4"])
    return None


def motion(scale, px, py) -> str:
    if scale is None:
        return ""
    return f"""
                <filter><effect>
                  <name>motion</name><effectid>motion</effectid>
                  <effectcategory>motion</effectcategory><effecttype>motion</effecttype><mediatype>video</mediatype>
                  <parameter authoringApp="PremierePro"><parameterid>position</parameterid><name>Position</name>
                    <value><horiz>{px}</horiz><vert>{py}</vert></value></parameter>
                  <parameter authoringApp="PremierePro"><parameterid>scale</parameterid><name>Scale</name>
                    <valuemin>0</valuemin><valuemax>600</valuemax><value>{scale}</value></parameter>
                  <parameter authoringApp="PremierePro"><parameterid>scaleLinkedToWidth</parameterid>
                    <name>Uniform Scale</name><value>TRUE</value></parameter>
                </effect></filter>"""


def vfile(cid, name, url, video=True) -> str:
    media = (f"<video><samplecharacteristics><width>{W}</width><height>{H}</height>"
             f"<pixelaspectratio>square</pixelaspectratio><fielddominance>none</fielddominance>"
             f"</samplecharacteristics></video>") if video else ""
    return (f'<file id="file-{cid}"><name>{html.escape(name)}</name>'
            f'<pathurl>{url}</pathurl><media>{media}</media></file>')


def vclip(cid, name, url, in_f, out_f, start_f, end_f, scale=None, px=None, py=None, is_image=False) -> str:
    dur = (end_f - start_f) if is_image else (out_f - in_f)
    in_v = 0 if is_image else in_f
    out_v = dur if is_image else out_f
    return f"""
              <clipitem id="clipitem-{cid}">
                <name>{html.escape(name)}</name>
                <duration>{dur}</duration>
                <rate><timebase>{FPS}</timebase><ntsc>FALSE</ntsc></rate>
                <in>{in_v}</in><out>{out_v}</out><start>{start_f}</start><end>{end_f}</end>
                {vfile(cid, name, url)}{motion(scale, px, py)}
              </clipitem>"""


def aclip(cid, name, url, in_f, out_f, start_f, end_f, ch) -> str:
    return f"""
              <clipitem id="clipitem-{cid}">
                <name>{html.escape(name)}</name>
                <duration>{out_f}</duration>
                <rate><timebase>{FPS}</timebase><ntsc>FALSE</ntsc></rate>
                <in>{in_f}</in><out>{out_f}</out><start>{start_f}</start><end>{end_f}</end>
                <sourcetrack><mediatype>audio</mediatype><trackindex>{ch}</trackindex></sourcetrack>
                <file id="file-audio-main"><name>{html.escape(name)}</name><pathurl>{url}</pathurl>
                  <media><audio><samplecharacteristics><samplerate>48000</samplerate><depth>16</depth>
                  </samplecharacteristics><channelcount>2</channelcount></audio></media></file>
              </clipitem>"""


def track(items: str) -> str:
    return f"\n        <track>\n          <enabled>TRUE</enabled>{items}\n        </track>"


def main(argv=None) -> int:
    argv = argv or sys.argv[1:]
    if not argv:
        print("usage: export_premiere_pro.py <project_dir>", file=sys.stderr); return 1
    pdir = Path(argv[0]).expanduser().resolve()
    video = pdir / "video.mp4"
    jet = json.loads((pdir / "jetcut_plan.json").read_text(encoding="utf-8"))
    plan = json.loads((pdir / "timing_plan.json").read_text(encoding="utf-8"))
    vurl = pathurl(video)

    # V1 + 配置情報（kept_chunks をジェットカット順に連結）
    v1, a1, a2 = "", "", ""
    placements = []  # {in_f,out_f,start_f,end_f}
    cum = 0
    for i, c in enumerate(jet["kept_chunks"]):
        in_f = round(c["start"] * FPS); out_f = round(c["end"] * FPS)
        dur = max(out_f - in_f, 1); start_f = cum; end_f = cum + dur; cum = end_f
        placements.append({"in_f": in_f, "out_f": out_f, "start_f": start_f, "end_f": end_f})
        v1 += vclip(f"v1-{i}", "video.mp4", vurl, in_f, out_f, start_f, end_f)
        a1 += aclip(f"a1-{i}", "video.mp4", vurl, in_f, out_f, start_f, end_f, 1)
        a2 += aclip(f"a2-{i}", "video.mp4", vurl, in_f, out_f, start_f, end_f, 2)
    total_frames = cum

    v2 = v3 = v4 = v5 = ""
    n_pic = n_sld = n_brl = 0
    slide_intervals = []
    for ev in plan["events"]:
        if ev["type"] not in ("picture", "slide", "broll") or "jc_end" not in ev:
            continue
        sf = round(ev["jc_start"] * FPS); ef = max(round(ev["jc_end"] * FPS), sf + 1)
        path = asset_for(pdir, ev["type"], ev["number"] or 1)
        if ev["type"] == "slide":
            slide_intervals.append((sf, ef))
        if not path:
            print(f"[warn] missing asset: {ev['type']}{ev['number']}", file=sys.stderr); continue
        url = pathurl(path); name = path.name
        if ev["type"] == "picture":
            v2 += vclip(f"v2-{n_pic}", name, url, 0, ef - sf, sf, ef, PICTURE_SCALE, *PICTURE_POS, is_image=True); n_pic += 1
        elif ev["type"] == "slide":
            v3 += vclip(f"v3-{n_sld}", name, url, 0, ef - sf, sf, ef, SLIDE_SCALE, *SLIDE_POS, is_image=True); n_sld += 1
        elif ev["type"] == "broll":
            v4 += vclip(f"v4-{n_brl}", name, url, 0, ef - sf, sf, ef); n_brl += 1

    # V5 ワイプ: スライド区間に重なる V1 配置を右上ワイプで複製
    wi = 0
    for (ss, se) in slide_intervals:
        for pl in placements:
            a = max(ss, pl["start_f"]); b = min(se, pl["end_f"])
            if b <= a:
                continue
            src_in = pl["in_f"] + (a - pl["start_f"])
            v5 += vclip(f"v5-{wi}", "video.mp4 (wipe)", vurl, src_in, src_in + (b - a), a, b,
                        WIPE_SCALE, *WIPE_POS); wi += 1

    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE xmeml>
<xmeml version="4">
  <sequence>
    <name>clbs-youtube-edit</name>
    <duration>{total_frames}</duration>
    <rate><timebase>{FPS}</timebase><ntsc>FALSE</ntsc></rate>
    <media>
      <video>
        <format><samplecharacteristics><width>{W}</width><height>{H}</height>
          <pixelaspectratio>square</pixelaspectratio><fielddominance>none</fielddominance></samplecharacteristics></format>{track(v1)}{track(v2)}{track(v3)}{track(v4)}{track(v5)}
      </video>
      <audio><numchannels>2</numchannels>{track(a1)}{track(a2)}
      </audio>
    </media>
  </sequence>
</xmeml>
"""
    out = pdir / "project_premiere.xml"
    out.write_text(xml, encoding="utf-8")
    print(f"[xml] V1={len(placements)} V2={n_pic} V3={n_sld} V4={n_brl} V5={wi}  frames={total_frames}")
    print(f"- {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
