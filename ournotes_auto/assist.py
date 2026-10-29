"""跟打：玩家自己选歌、点开始，演奏时自动打歌。

不做任何界面操作（选歌、LIVE START、演出前选项、结算页翻页、弹窗都由玩家自己点），只截图看画面；
唯一的点击是在结算页上点 ⇄ 切换到 FAST/SLOW，读完判定数用来记录和修正 offset。

乐队确认页（认出曲目、下载好谱面）→ 玩家点 LIVE START（→ 演出前选项设置 → 玩家点「演出」）→ 加载画面出现时
就开始等首音符并演奏 → 等玩家点过 LIVE CLEAR，在结算页读成绩。

协力演出（和另外 4 个玩家一起打）在选择难度页认曲目：难度就在这一页上选，玩家多半是进来之后才改，所以每次
看到这一页都核对一次难度，改了就重新认。之后同样等玩家点「准备完成」，等其他玩家准备、加载这段画面认不出，
和单人一样提前开始同步：同步最多等 ``play.sync.timeout_s``（默认 40s），其他玩家久久不准备时这一局就不打了。
协力不能暂停，右上角没有暂停按钮，所以同步完不确认画面、演奏中也不检查（见 PlaySession.play 的 confirm_screen
与 guard），打的是不是这一首只能靠同步结果判断。

开始看画面的时机只能猜：离开乐队确认页后画面认不出 LOADING_S 秒就当作在加载，和 ``run`` 一样穿过加载画面开始
同步——基线在加载时（画面静止）就取好，演奏画面一出来就能量到第一个音符。不等认出右上角的暂停按钮才开始：
按钮是淡入的，认出来时歌往往已经开始、第一个音符已经落下，跟踪区再也等不到静止（实测每局都卡在这里）。
认不出的头 LOOK_GAP_S 秒用 OCR 看一眼，回到了乐曲选择页等认得出的画面就不等了（弹窗淡入时的短暂认不出也靠它）。
同步完成后先确认真的在演奏画面（``confirm_screen``）才发触控。演奏中暂停、在确认弹窗里点了重试时，歌曲立即
从头开始，用这首的谱面接着打（点「继续」时打不了，接不上中途）。
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time
from typing import TYPE_CHECKING

from .charts.bdon import BdonClient, ChartNotFound
from .charts.catalog import Catalog, Song
from .charts.model import Chart
from .config import Config
from .nav.screens import Screen, multi_song
from .player.guard import LifeDepleted, PlayInterrupted
from .player.session import PlaySession, SyncFailed
from .player.sync import SyncTimeout
from .records import RecordStore
from .runner import NavigationError, RunStats, SongLabel, chart_offset_ms, identify_song, record_result
from .sources import DIFFICULTIES

if TYPE_CHECKING:
    from .nav.navigator import GameNavigator
    from .stats import Tracker

logger = logging.getLogger(__name__)

# 认曲目的页面：自由演出 / 挑战演出的乐队确认页，协力演出的选择难度页
BAND_SCREENS = (Screen.BAND_CONFIRM, Screen.CHALLENGE_BAND_CONFIRM, Screen.MULTI_DIFFICULTY)
# 乐队确认页上弹出的窗口：关掉后还是同一首歌，不用重新认曲目
BAND_DIALOGS = (
    Screen.LIVE_OPTIONS,
    Screen.LB_SETTING,
    Screen.LB_RECOVER,
    Screen.LB_RECOVER_CONFIRM,
    Screen.LB_RECOVERED,
    Screen.CP_SETTING,
    Screen.POPUP,
)
POLL_S = 0.3  # 平时隔这么久看一次画面
PLAY_POLL_S = 0.03  # 等演奏画面时隔这么久看一次暂停按钮（不做 OCR，很快）
LOOK_GAP_S = 0.8  # 离开乐队确认页后隔这么久用 OCR 看一眼是不是回到了别的画面（弹窗淡入、玩家反悔）
LOADING_S = 1.5  # 画面认不出这么久就当作在加载，开始同步（加载一般 7~10s，弹窗淡入不到 1s）
DIFF_MISSES = 3  # 协力演出：连着这么多次读不出选中的难度就撤掉认好的谱面（见 _handle）


class Assist:
    def __init__(
        self,
        config: Config,
        nav: GameNavigator,
        session: PlaySession,
        client: BdonClient,
        catalog: Catalog,
        store: RecordStore,
        stop: threading.Event | None = None,
        tracker: Tracker | None = None,
    ):
        self.cfg = config
        self.nav = nav
        self.session = session
        self.client = client
        self.catalog = catalog
        self.store = store
        self.stop = stop or threading.Event()
        self.tracker = tracker  # 统计页（出错只记警告）
        self.stats = RunStats()
        self.clock = time.monotonic
        self._on_band = False  # 最近认出的是乐队确认页（或它上面的弹窗），已经认过曲目
        self._armed: Chart | None = None  # 演奏画面出现时要打的谱面
        self._multi = False  # _armed 是在协力演出的选择难度页认的（没有暂停按钮，见 _play_once）
        self._retry = False  # _armed 是暂停重试的那一首（歌曲立即从头开始）
        self._misses = 0  # 协力演出的选择难度页上连着几次没读出选中的难度
        self._last: Chart | None = None  # 上一局的谱面（暂停重试时接着打）

    def run(self) -> RunStats:
        logger.info("跟打：请自己选歌、开始演出，演奏时自动打歌（在乐队确认页认曲目，按停止结束）")
        if self.tracker is not None:
            self.tracker.start()
        self._status("assist")
        try:
            while not self.stop.is_set():
                try:
                    self._step()
                except NavigationError as e:
                    if self.stop.is_set():
                        break
                    logger.warning("%s", e)
                    self._sleep(POLL_S)
        finally:  # 按停止、Ctrl+C 中断时也记下这次运行
            if self.tracker is not None:
                self.tracker.finish(self.stats)
        logger.info(
            "共演奏 %d 局：FC %d，AP %d，没跟上 %d",
            self.stats.plays,
            self.stats.full_combo,
            self.stats.all_perfect,
            self.stats.failures,
        )
        return self.stats

    def _sleep(self, seconds: float) -> None:
        self.stop.wait(seconds)

    def _status(self, kind: str, detail: str = "") -> None:
        """统计页上显示正在做什么（见 :meth:`.stats.Tracker.status`）：演奏中，或等玩家开始。"""
        if self.tracker is not None:
            self.tracker.status(kind, detail)

    def _step(self) -> None:
        self._handle(*self.nav.look(still_ok=True, blocking_ok=True))
        self._sleep(POLL_S)

    def _handle(self, screen: Screen, items) -> None:
        if screen is Screen.UNKNOWN:
            if self._armed is None:
                return
            chart, retry, multi = self._armed, self._retry, self._multi
            seen = self._await_play()
            if seen is None:
                self._armed, self._retry, self._on_band = None, False, False
                self._play(chart, retry, multi)
            elif seen[0] is not Screen.UNKNOWN:
                self._handle(*seen)  # 等演奏画面时回到了认得出的画面（如乐曲选择页），照常处理
        elif screen in BAND_SCREENS:
            multi = screen is Screen.MULTI_DIFFICULTY
            # 协力演出的难度就在这一页上选，玩家多半是进来之后才改：难度变了（或者刚才没认出来）就重新认。
            # 封面左边那个竖排大字偶尔读不出来（整帧 OCR 的检测时好时坏），这时不能当作玩家没改——
            # 连着 DIFF_MISSES 次都读不出就撤掉认好的谱面，宁可这一局不打，也别拿改之前那个难度的谱面打
            # 实测：玩家从 HARD 改到 EXPERT 后一直没读出来，整局是按 HARD 的谱面打的
            if multi and self._on_band:
                _, diff, _ = multi_song(items, self.nav.frame)
                if diff is None:
                    self._misses += 1
                    if self._misses >= DIFF_MISSES and self._armed is not None:
                        logger.warning(
                            "连着 %d 次没读出选中的难度，撤掉 %s：难度可能已经改了，这一局不打",
                            self._misses,
                            self._label(self._armed),
                        )
                        self._armed = None
                else:
                    self._misses = 0
                    if self._armed is None or diff != self._armed.difficulty:
                        self._on_band = False
            if not self._on_band:
                self._on_band, self._retry, self._misses = True, False, 0
                self._last = None  # 换了歌：认不出这首时，暂停重试也不能拿上一首的谱面打
                self._multi = multi
                self._armed = self._prepare()
        elif screen in BAND_DIALOGS:
            if screen is Screen.LIVE_OPTIONS:
                self._retry = False  # 重试后又弹出了演出前选项设置：之后和开始时一样要加载
                if self.nav.prev_screen is not Screen.LIVE_OPTIONS:
                    self.nav.check_speed(items)
        elif screen is Screen.RETRY_CONFIRM:
            if self._last is not None and not self._retry:
                logger.info("确认重试后接着打 %s", self._label(self._last))
            self._on_band = False
            self._armed, self._retry = self._last, self._last is not None
        else:
            self._on_band = False
            self._armed, self._retry = None, False

    def _prepare(self) -> Chart | None:
        """认出乐队确认页上的曲目并下载谱面；认不出、下载失败时返回 None（这一首不打）。"""
        try:
            song, diff, basis = self._identify(self.nav.selected_song())
            chart = self.client.chart(song.music_id, diff, song.display_title())
        except (NavigationError, ChartNotFound, ConnectionError) as e:
            logger.warning("这一首不打：%s", e)
            return None
        logger.info("曲目：%s（%d，%s，%s），开始演出后自动打歌", song.display_title(), song.music_id, diff, basis)
        return chart

    def _identify(self, label: SongLabel) -> tuple[Song, str, str]:
        """(曲目, 难度, 依据)。跟打没有难度选项：画面上的难度读不出时，按等级在四个难度里找唯一对得上的。"""
        if label.difficulty is not None:
            song, basis = identify_song(self.catalog, label, label.difficulty)
            return song, label.difficulty, basis
        if label.level is None:
            raise NavigationError(f"乐队确认页上的难度和等级都没读出来（曲名 {label.title!r}）")
        found = []
        for diff in DIFFICULTIES:
            try:
                song, basis = identify_song(self.catalog, label, diff)
            except NavigationError:
                continue
            if song.level(diff) == label.level:
                found.append((song, diff, basis))
        if len({(song.music_id, diff) for song, diff, _ in found}) != 1:
            raise NavigationError(f"乐队确认页上的难度没读出来，按等级 {label.level} 也定不下来（曲名 {label.title!r}）")
        return found[0]

    def _await_play(self) -> tuple[Screen, list] | None:
        """等到该开始同步：看到了演奏画面的暂停按钮，或者画面认不出 ``LOADING_S`` 秒（在加载了），
        两者都返回 None。回到了认得出的别的画面时返回 (画面, 识别结果)，按了停止时返回 (UNKNOWN, [])。"""
        start = self.clock()
        next_look = start + LOOK_GAP_S
        while not self.stop.is_set():
            if self.nav.poll_play():
                return None
            t = self.clock()
            if t - start > LOADING_S:
                logger.debug("画面认不出 %.1fs，当作在加载，开始同步", t - start)
                return None
            if t >= next_look:
                screen, items = self.nav.look(still_ok=True, blocking_ok=True)
                if screen is not Screen.UNKNOWN:
                    return screen, items
                next_look = self.clock() + LOOK_GAP_S
            self._sleep(PLAY_POLL_S)
        return Screen.UNKNOWN, []

    @staticmethod
    def _label(chart: Chart) -> str:
        return f"{chart.title or chart.music_id} {chart.difficulty.upper()}"

    def _play(self, chart: Chart, retry: bool, multi: bool = False) -> None:
        self._status("play", self._label(chart))
        try:
            self._play_once(chart, retry, multi)
        finally:
            self._status("assist")
            if self.tracker is not None:
                self.tracker.update(self.stats)

    def _play_once(self, chart: Chart, retry: bool, multi: bool = False) -> None:
        """``multi``：协力演出。右上角没有暂停按钮（协力不能暂停），所以同步完不确认画面、演奏中也不看它，
        只靠同步结果判断打的是不是这一首。"""
        self._last = chart
        self.session.learned_offset_ms = self.store.learned_offset_ms + chart_offset_ms(self.cfg, self.store, chart)
        try:
            outcome = self.session.play(
                chart, self.stop, retry=retry, confirm_screen=not multi, guard=not multi
            )
        except (SyncFailed, SyncTimeout, LifeDepleted) as e:
            self.stats.failures += 1
            hint = "" if multi else "（可以暂停 → 重试，会接着打）"
            logger.warning("%s：%s，这一局不打了%s", self._label(chart), e, hint)
            return
        except PlayInterrupted as e:
            logger.warning("%s：%s", self._label(chart), e)
            return
        except InterruptedError:  # 同步时按了停止
            return
        except (OSError, RuntimeError, subprocess.SubprocessError) as e:
            # 触控后端断了（别的程序重启了 adb server 等）：只算这一局失败，下一局重开时会自己连回来
            self.stats.failures += 1
            logger.warning("%s：触控出错，这一局不打了（%s）", self._label(chart), e)
            logger.debug("触控出错", exc_info=True)
            return
        try:
            counts = self.nav.read_result(chart.judged_count or None)
        except (OSError, RuntimeError, subprocess.SubprocessError) as e:
            if not self.stop.is_set():
                logger.warning("没读到这一局的结算：%s", e)
            return
        result = record_result(self.cfg, self.store, outcome, counts)
        self.stats.plays += 1
        self.stats.full_combo += bool(result.full_combo)
        self.stats.all_perfect += bool(result.all_perfect)
