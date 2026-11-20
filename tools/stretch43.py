"""Synthetic 4:3 stretched recordings, for testing the scanner's stretched layout (developer tool, not in the exe).

A player who runs Apex at 1440 x 1080 (4:3) stretched to a 16:9 screen gets a HUD that keeps every element's left /
right position and width but squashes its height and its distance from the top or bottom edge to 75 %; elements in
the middle of the screen stay centred. The game draws that HUD at the 4:3 render size, and the recording scales the
1440 x 1080 picture up to 16:9, so text is also softer.

This rebuilds such a picture from a 16:9 one, band by band:
  top band     rows    0..640  -> 0..480     (anchored to the top edge)
  middle band  rows  400..1040 -> 480..960   (centred)
  bottom band  rows  800..1440 -> 960..1440  (anchored to the bottom edge)
then down to 1440 x 1080 (the game's render size) and back up to 2560 x 1440 (the recording). The 3D scene between
the bands is cut, which does not matter for the HUD readers; the full map and the drop screen are not rebuilt the way
the game draws them (use real frames for those).

  python tools/stretch43.py video <16:9 recording> <out.mp4> [--start s] [--end s]
      keyframes only, each one stretched, all-intra H.264, same times, game audio track copied
  python tools/stretch43.py frame <in.png> <out.png>
"""
import os, sys, argparse, subprocess
import numpy as np
from PIL import Image

W, H = 2560, 1440
BANDS = ((0, 640, 0), (400, 1040, 480), (800, 1440, 960))     # source rows y0..y1 -> output rows from y
FILTER = ('split=3[a][b][c];' + ';'.join(f'[{k}]crop={W}:640:0:{y0},scale={W}:480[{k}o]'
                                        for k, (y0, y1, _) in zip('abc', BANDS))
          + ';[ao][bo][co]vstack=3,scale=1440:1080:flags=area,scale=2560:1440:flags=bicubic')


def stretch(img):
    """16:9 frame (1440 x 2560 x 3, uint8) -> the same scene as a 4:3 stretched recording would show its HUD."""
    im = Image.fromarray(np.ascontiguousarray(img))
    out = Image.new('RGB', (W, H))
    for y0, y1, y in BANDS:
        out.paste(im.crop((0, y0, W, y1)).resize((W, 480), Image.BILINEAR), (0, y))
    return np.asarray(out.resize((1440, 1080), Image.BOX).resize((W, H), Image.BICUBIC))


def video(src, dst, start=0.0, end=None):
    cmd = ['ffmpeg', '-hide_banner', '-v', 'error', '-y', '-skip_frame', 'nokey', '-copyts']
    if start: cmd += ['-ss', f'{start:.2f}']
    cmd += ['-i', src]
    if end: cmd += ['-to', f'{end:.2f}']
    cmd += ['-map', '0:v:0', '-map', '0:a:0?', '-vf', f'scale={W}:{H},' + FILTER, '-fps_mode', 'passthrough',
            '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '18', '-g', '1', '-pix_fmt', 'yuv420p', '-c:a', 'copy', dst]
    subprocess.check_call(cmd)


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest='cmd', required=True)
    v = sp.add_parser('video'); v.add_argument('src'); v.add_argument('dst')
    v.add_argument('--start', type=float, default=0.0); v.add_argument('--end', type=float)
    f = sp.add_parser('frame'); f.add_argument('src'); f.add_argument('dst')
    A = ap.parse_args()
    if A.cmd == 'video': video(A.src, A.dst, A.start, A.end)
    else: Image.fromarray(stretch(np.asarray(Image.open(A.src).convert('RGB')))).save(A.dst)
