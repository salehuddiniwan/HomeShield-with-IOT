"""Video files as cameras: played in real time, skipping the frames analysis
can't keep up with, each analysed frame timed by the file's own clock."""

import time
from types import SimpleNamespace

import numpy as np

from homeshield.cameras import CaptureWorker


class FakeFile:
    """A 30 FPS clip whose pixels hold the frame number."""

    def __init__(self, frames=600, fps=30.0):
        self.n, self.fps, self.pos = frames, fps, 0

    def get(self, prop):
        return self.fps

    def grab(self):
        if self.pos >= self.n:
            return False
        self.pos += 1
        return True

    def read(self):
        if self.pos >= self.n:
            return False, None
        self.pos += 1
        return True, np.full((2, 2, 3), (self.pos - 1) % 256, np.uint8)

    def set(self, prop, value):
        self.pos = int(value)

    def release(self):
        pass


def _play(seconds_per_frame, frames=10):
    worker = CaptureWorker({"camera_id": 1, "name": "clip", "url": "clip.mp4"},
                           SimpleNamespace(settings={}), None, None)
    seen = []

    def analyse(frame, ts):
        seen.append((int(frame[0, 0, 0]), ts))
        worker._last_ts = ts
        time.sleep(seconds_per_frame)
        if len(seen) >= frames:
            worker.request_stop()

    worker._handle = analyse
    start = time.time()
    worker._loop_file(FakeFile())
    return seen, time.time() - start


def test_a_file_analysed_slower_than_it_plays_skips_frames_and_stays_in_real_time():
    seen, elapsed = _play(0.1)                    # 10 analyses a second, a 30 FPS clip
    numbers = [n for n, _ in seen]
    steps = [b - a for a, b in zip(numbers, numbers[1:])]
    assert min(steps) >= 2 and max(steps) <= 6    # about every third frame
    # Each analysed frame is timed by the file: 3 frames on is 0.1 s later.
    for (n1, t1), (n2, t2) in zip(seen, seen[1:]):
        assert abs((t2 - t1) - (n2 - n1) / 30) < 1e-6
    assert abs(numbers[-1] / 30 - elapsed) < 0.3   # where the clip is, is where the clock is


def test_a_file_that_keeps_up_plays_every_frame():
    seen, elapsed = _play(0.0, frames=12)
    assert [n for n, _ in seen] == list(range(12))
    assert elapsed >= 11 / 30 - 0.02               # not faster than real time either
