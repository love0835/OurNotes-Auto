"""用假对象验证跟打（玩家自己操作，只帮忙打歌）的画面判断。"""

import threading

import pytest

from ournotes_auto.assist import DIFF_MISSES, Assist
from ournotes_auto.charts.catalog import Catalog, Song
from ournotes_auto.charts.model import Chart
from ournotes_auto.config import Config
from ournotes_auto.nav.screens import Screen
from ournotes_auto.player.executor import ExecStats
from ournotes_auto.player.guard import PlayInterrupted
from ournotes_auto.player.session import PlayOutcome, SyncFailed
from ournotes_auto.player.sync import SyncResult
from ournotes_auto.records import RecordStore
from ournotes_auto.result_reader import OcrItem, ResultCounts
from ournotes_auto.runner import NavigationError, SongLabel

S = Screen
PLAY = "play"  # 脚本里的这一项：等演奏画面时看到了暂停按钮


def multi_items(difficulty: str) -> list[OcrItem]:
    """协力演出选择难度页上封面左边那个竖排的大字难度（选中的那个）。"""
    return [OcrItem(790, 172, 52, 115, difficulty.upper())]


class FakeNav:
    """``script``：依次的画面；``PLAY`` 表示等演奏画面（poll_play）时看到了暂停按钮。脚本用完就停止。"""

    def __init__(self, script, stop, label=SongLabel("诗超绊", "expert"), result_error=None):
        self.script = list(script)
        self.stop = stop
        self.label = label
        self.result_error = result_error
        self.prev_screen = self.last_screen = S.UNKNOWN
        self.calls = []

    def _next(self):
        if not self.script:
            self.stop.set()
            return S.UNKNOWN
        return self.script.pop(0)

    def look(self, still_ok=False, blocking_ok=False):
        assert still_ok and blocking_ok  # 跟打不能因为画面不动、维护页而重启游戏
        screen = self._next()
        items = []
        if isinstance(screen, tuple):  # 脚本里写成 (画面, 识别结果)
            screen, items = screen
        if screen == PLAY:
            screen = S.UNKNOWN  # 演奏画面 OCR 认不出
        self.prev_screen, self.last_screen = self.last_screen, screen
        return screen, items

    def poll_play(self):
        if self.script and self.script[0] == PLAY:
            self.script.pop(0)
            return True
        if not self.script:
            self.stop.set()
        return False

    def selected_song(self):
        self.calls.append("identify")
        return self.label

    def check_speed(self, items):
        self.calls.append("speed")

    def read_result(self, expected_total=None):
        self.calls.append("result")
        if self.result_error:
            raise NavigationError(self.result_error)
        return ResultCounts(
            counts={"perfect": 1000, "great": 27, "good": 0, "bad": 0, "miss": 0},
            fast={"perfect": 300, "great": 20, "good": 0, "bad": 0, "miss": 0},
            slow={"perfect": 700, "great": 7, "good": 0, "bad": 0, "miss": 0},
            score=123,
            combo=1027,
        )


class FakeClient:
    def chart(self, mid, diff, title=""):
        return Chart(mid, diff, title)


class FakeSession:
    def __init__(self, fails=0, interrupt=False):
        self.fails, self.interrupt = fails, interrupt
        self.learned_offset_ms = 0.0
        self.plays = []  # (谱面, retry, confirm_screen, guard)

    def play(self, chart, stop=None, retry=False, confirm_screen=False, guard=True):
        self.plays.append((chart.key, retry, confirm_screen, guard))
        sync = SyncResult(1.0, 0.0, [], -40.0, 0.835, 1.0, 1.0, 190.0, self.fails <= 0)
        self.fails -= 1
        if self.fails >= 0:
            raise SyncFailed("假失败", sync)
        if self.interrupt:
            raise PlayInterrupted("演奏中途离开了演奏画面")
        return PlayOutcome(chart, None, sync, ExecStats(sent=10, lateness_ms=[0.2] * 5), 3.0)


class FakeTracker:
    """统计页：记下开始、实时状态、每局后的刷新和结束。"""

    def __init__(self):
        self.calls = []

    def start(self):
        self.calls.append("start")

    def status(self, kind, detail="", **extra):
        self.calls.append(f"status:{kind}")

    def update(self, stats):
        self.calls.append(f"update:{stats.plays}")

    def finish(self, stats):
        self.calls.append(f"finish:{stats.plays}")


def make(tmp_path, script, session=None, tracker=None, **kw):
    stop = threading.Event()
    catalog = Catalog(
        {
            100008: Song(
                100008,
                ["詩超絆", "Utachou", "詩超絆", "诗超绊"],
                difficulties={"easy": {"level": 7}, "normal": {"level": 13}, "hard": {"level": 19}, "expert": {"level": 26}},
            )
        }
    )
    nav = FakeNav(script, stop, **kw)
    store = RecordStore(tmp_path)
    assist = Assist(Config(), nav, session or FakeSession(), FakeClient(), catalog, store, stop, tracker)
    assist._sleep = lambda s: None
    t = [0.0]

    def clock():
        t[0] += 0.1
        return t[0]

    assist.clock = clock
    return assist, nav, store


def test_plays_after_player_starts(tmp_path):
    """乐队确认页认一次曲目；玩家点开始（演出前选项设置 → 加载）后看到演奏画面才开始打，打完读结算并记录。"""
    script = [S.SONG_SELECT, S.BAND_CONFIRM, S.BAND_CONFIRM, S.LIVE_OPTIONS, S.LIVE_OPTIONS, S.UNKNOWN, PLAY, S.RESULT]
    assist, nav, store = make(tmp_path, script)
    stats = assist.run()
    assert nav.calls == ["identify", "speed", "result"]
    assert assist.session.plays == [("100008_expert", False, True, True)]  # 先确认在演奏画面上再发触控
    assert stats.plays == 1 and stats.full_combo == 1
    assert len(store.history()) == 1 and store.history()[0].fast == 320


def test_tracker_records_run(tmp_path):
    """统计页：开始时 start，演奏时显示「演奏中」、打完回到「等你开始」并刷新局数，结束时 finish。"""
    script = [S.BAND_CONFIRM, S.UNKNOWN, PLAY, S.RESULT]
    tracker = FakeTracker()
    assist, _, _ = make(tmp_path, script, tracker=tracker)
    assist.run()
    assert tracker.calls == ["start", "status:assist", "status:play", "status:assist", "update:1", "finish:1"]


def test_back_to_song_select_disarms(tmp_path):
    """离开乐队确认页后回到了乐曲选择页（没开始演出）：不打；换了一首歌要重新认。"""
    script = [S.BAND_CONFIRM, S.UNKNOWN, S.SONG_SELECT, S.BAND_CONFIRM, S.UNKNOWN, PLAY]
    assist, nav, _ = make(tmp_path, script)
    assist.run()
    assert nav.calls == ["identify", "identify", "result"]
    assert len(assist.session.plays) == 1


def test_multi_live(tmp_path):
    """协力演出：在选择难度页认曲目，之后和单人一样（等其他玩家准备、加载时画面认不出）。"""
    script = [S.MULTI_DIFFICULTY, S.MULTI_DIFFICULTY, S.UNKNOWN, PLAY, S.RESULT]
    assist, nav, store = make(tmp_path, script)
    stats = assist.run()
    assert nav.calls == ["identify", "result"]  # 只认一次：难度没变
    # 协力演出没有暂停按钮：同步完不确认画面，演奏中也不看它
    assert assist.session.plays == [("100008_expert", False, False, False)]
    assert stats.plays == 1 and len(store.history()) == 1


def test_multi_live_difficulty_changed(tmp_path):
    """协力演出的难度就在选择难度页上选：玩家认完之后才改难度，要按改后的难度重新认。"""
    script = [
        (S.MULTI_DIFFICULTY, multi_items("easy")),
        (S.MULTI_DIFFICULTY, multi_items("easy")),  # 没改：不重新认
        (S.MULTI_DIFFICULTY, multi_items("expert")),
        S.UNKNOWN,
        PLAY,
        S.RESULT,
    ]
    labels = iter([SongLabel("诗超绊", "easy"), SongLabel("诗超绊", "expert")])
    assist, nav, _ = make(tmp_path, script)
    nav.selected_song = lambda: (nav.calls.append("identify"), next(labels))[1]
    assist.run()
    assert nav.calls.count("identify") == 2
    assert [key for key, *_ in assist.session.plays] == ["100008_expert"]


def test_multi_live_difficulty_unreadable(tmp_path):
    """协力演出：竖排大字连着几次读不出，难度可能已经改了——撤掉认好的谱面，这一局不打。"""
    script = [
        (S.MULTI_DIFFICULTY, multi_items("expert")),
        *[(S.MULTI_DIFFICULTY, [])] * DIFF_MISSES,
        S.UNKNOWN,
        PLAY,
    ]
    assist, nav, _ = make(tmp_path, script)
    assist.run()
    assert nav.calls == ["identify"]
    assert assist.session.plays == []  # 宁可不打，也不拿改之前那个难度的谱面打


def test_multi_live_difficulty_miss_tolerated(tmp_path):
    """偶尔一帧读不出大字（整帧 OCR 的检测时好时坏）不算改难度，照原来认好的谱面打。"""
    script = [
        (S.MULTI_DIFFICULTY, multi_items("expert")),
        (S.MULTI_DIFFICULTY, []),
        (S.MULTI_DIFFICULTY, multi_items("expert")),
        S.UNKNOWN,
        PLAY,
        S.RESULT,
    ]
    assist, nav, _ = make(tmp_path, script)
    assist.run()
    assert nav.calls == ["identify", "result"]
    assert [key for key, *_ in assist.session.plays] == ["100008_expert"]


def test_dialog_fade_keeps_song(tmp_path):
    """点 LIVE START 后弹窗淡入时短暂认不出画面：回到乐队确认页也不重新认曲目。"""
    script = [S.BAND_CONFIRM, S.UNKNOWN, S.BAND_CONFIRM, S.LB_SETTING, S.BAND_CONFIRM, S.UNKNOWN, PLAY]
    assist, nav, _ = make(tmp_path, script)
    assist.run()
    assert nav.calls == ["identify", "result"]
    assert len(assist.session.plays) == 1


def test_unknown_without_band_does_nothing(tmp_path):
    """没经过乐队确认页（认不出曲目）就进了演奏画面：不打。"""
    script = [S.HOME, S.UNKNOWN, PLAY, S.UNKNOWN]
    assist, nav, _ = make(tmp_path, script)
    assist.run()
    assert assist.session.plays == [] and nav.calls == []


def test_retry_after_sync_failure(tmp_path):
    """同步没跟上：这一局不读结算；玩家暂停 → 重试 → 确认后接着打同一首（沿用基线）。"""
    script = [S.BAND_CONFIRM, S.UNKNOWN, PLAY, S.UNKNOWN, S.PAUSE, S.RETRY_CONFIRM, S.UNKNOWN, PLAY, S.RESULT]
    assist, nav, store = make(tmp_path, script, session=FakeSession(fails=1))
    stats = assist.run()
    assert assist.session.plays == [("100008_expert", False, True, True), ("100008_expert", True, True, True)]
    assert nav.calls == ["identify", "result"]
    assert stats.plays == 1 and stats.failures == 1 and len(store.history()) == 1


def test_pause_continue_does_not_replay(tmp_path):
    """暂停后点继续（没有确认重试）：歌从中途接着放，接不上，不打。"""
    script = [S.BAND_CONFIRM, S.UNKNOWN, PLAY, S.PAUSE, S.UNKNOWN, PLAY, S.UNKNOWN]
    assist, _, _ = make(tmp_path, script, session=FakeSession(interrupt=True))
    assist.run()
    assert len(assist.session.plays) == 1


def test_retry_cancelled(tmp_path):
    """确认重试的弹窗里点了取消（回到暂停菜单）：不打。"""
    script = [S.BAND_CONFIRM, S.UNKNOWN, PLAY, S.PAUSE, S.RETRY_CONFIRM, S.PAUSE, S.UNKNOWN, PLAY]
    assist, _, _ = make(tmp_path, script, session=FakeSession(interrupt=True))
    assist.run()
    assert len(assist.session.plays) == 1


def test_unidentified_song_not_retried_with_old_chart(tmp_path):
    """上一首打过，这一首认不出：暂停重试时不能拿上一首的谱面打。"""
    script = [S.BAND_CONFIRM, S.UNKNOWN, PLAY, S.RESULT, S.SONG_SELECT, S.BAND_CONFIRM, S.UNKNOWN, PLAY]
    script += [S.PAUSE, S.RETRY_CONFIRM, S.UNKNOWN, PLAY]
    assist, nav, _ = make(tmp_path, script)
    labels = iter([SongLabel("诗超绊", "expert"), SongLabel("没有这首歌", "expert")])
    nav.selected_song = lambda: next(labels)
    assist.run()
    assert len(assist.session.plays) == 1


def test_result_missed_is_not_fatal(tmp_path):
    """玩家很快点掉了结算页：这一局不记录，接着等下一首。"""
    script = [S.BAND_CONFIRM, S.UNKNOWN, PLAY, S.SONG_SELECT, S.BAND_CONFIRM, S.UNKNOWN, PLAY]
    assist, nav, store = make(tmp_path, script, result_error="读完结算之前离开了结算页")
    stats = assist.run()
    assert len(assist.session.plays) == 2 and stats.plays == 0
    assert store.history() == []


def test_syncs_through_loading(tmp_path):
    """离开乐队确认页后画面认不出（在加载）：不等暂停按钮认出来，过 LOADING_S 就开始同步。

    暂停按钮是淡入的，等认出来时歌往往已经开始、第一个音符已经落下，跟踪区再也等不到静止。
    """
    script = [S.BAND_CONFIRM] + [S.UNKNOWN] * 50
    assist, nav, _ = make(tmp_path, script)
    assist.run()
    assert assist.session.plays == [("100008_expert", False, True, True)]  # 仍然先确认在演奏画面再发触控
    assert nav.calls == ["identify", "result"]


@pytest.mark.parametrize(
    ("label", "want"),
    [
        (SongLabel("诗超绊", None, 26), "100008_expert"),
        (SongLabel("诗超绊", None, 19), "100008_hard"),
        (SongLabel("诗超绊", None, 30), None),  # 等级对不上任何难度
        (SongLabel("诗超绊", None, None), None),  # 难度、等级都没读到
    ],
)
def test_difficulty_from_level(tmp_path, label, want):
    """跟打没有难度选项：画面上的难度读不出时按等级定难度，定不下来就不打。"""
    assist, _, _ = make(tmp_path, [S.BAND_CONFIRM, S.UNKNOWN, PLAY], label=label)
    assist.run()
    assert [key for key, *_ in assist.session.plays] == ([want] if want else [])
