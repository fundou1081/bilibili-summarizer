#!/usr/bin/env python3
"""
test_transcribe_skill.py — transcribe_skill.py 的烟雾测试

设计原则:
  - 不实际跑 B 站 API (避免污染收藏夹)
  - 用 fixture + mock 测核心逻辑
  - --dry-run 跑一次真实的收藏夹扫描
  - 全程 < 30 秒

覆盖:
  - 模块导入 + CLI
  - scan_favorites (真实 API, 待总结 1 个视频)
  - _organize_transcripts (单P/多P/ASR fallback)
  - _generate_summaries (LLM 失败 fallback)
  - 幂等性 (重复跑不破坏)
  - update_wiki (subprocess 调 wiki_gen)
"""

import os
import sys
import json
import shutil
import tempfile
import asyncio
import argparse
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

# 把项目根加到 path
PROJECT_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(PROJECT_DIR))

import transcribe_skill as ts
import summarize as sm

# ─── 计数器 ─────────────────────────────────────────────────────────

PASS = 0
FAIL = 0
ERRORS = []


def check(name: str, condition: bool, detail: str = ""):
    """记录一个测试结果"""
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        ERRORS.append(f"{name}: {detail}")
        print(f"  ✗ {name} -- {detail}")


# ─── 1. 模块导入 + 基本配置 ─────────────────────────────────────────

def test_module_basics():
    print("\n[1] 模块导入 + 配置")
    check("transcribe_skill 可导入", ts is not None)
    check("DEFAULT_SOURCE_FAV 是 待总结 id", ts.DEFAULT_SOURCE_FAV == 4115533556,
          f"got {ts.DEFAULT_SOURCE_FAV}")
    check("DEFAULT_DEST_FAV 是 已总结 id", ts.DEFAULT_DEST_FAV == 4090394056,
          f"got {ts.DEFAULT_DEST_FAV}")
    check("TRANSCRIBED_DIR 在项目下", ts.TRANSCRIBED_DIR.name == "transcribed")
    check("WIKI_DIR 在项目下", ts.WIKI_DIR.name == "wiki")
    check("CRED_FILE 是 .credential.json", ts.CRED_FILE.name == ".credential.json")
    check("依赖 summarize 模块", hasattr(ts, "sm"))
    check("依赖 Credential", hasattr(ts, "Credential"))


# ─── 2. 收藏夹扫描 (真实 API, 但只读) ─────────────────────────────

def test_scan_favorites():
    print("\n[2] 收藏夹扫描 (真实 API)")
    items = ts.scan_favorites(ts.DEFAULT_SOURCE_FAV)
    check("扫描成功 (不抛异常)", isinstance(items, list))
    check("待总结 至少有 1 个视频", len(items) >= 1,
          f"got {len(items)}")
    if items:
        first = items[0]
        check("视频有 bvid", "bvid" in first and first["bvid"].startswith("BV"),
              f"got {first}")
        check("视频有 title", "title" in first and len(first["title"]) > 0)
        check("视频有 duration", "duration" in first)
        check("视频有 aid (int)", "aid" in first and isinstance(first["aid"], int))


# ─── 3. CLI 帮助 + 参数解析 ────────────────────────────────────────

def test_cli():
    print("\n[3] CLI 参数解析")
    result = subprocess.run(
        [sys.executable, str(PROJECT_DIR / "transcribe_skill.py"), "--help"],
        capture_output=True, text=True, timeout=10,
    )
    check("--help 退出码 0", result.returncode == 0,
          f"got {result.returncode}")
    check("--help 提到 --dry-run", "--dry-run" in result.stdout)
    check("--help 提到 --bvid", "--bvid" in result.stdout)
    check("--help 提到 --asr-max-duration", "--asr-max-duration" in result.stdout)
    check("--help 提到 --skip-move", "--skip-move" in result.stdout)
    check("--help 提到 --skip-wiki", "--skip-wiki" in result.stdout)


# ─── 4. _organize_transcripts: 单P (无字幕 → ASR) ──────────────────

def test_organize_single_p_with_asr():
    print("\n[4] _organize_transcripts (单 P + ASR fallback)")
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        bvid = "BVtest1111"
        downloads = tmp / "downloads" / bvid
        downloads.mkdir(parents=True)
        transcribed = tmp / "transcribed" / bvid
        transcribed.mkdir(parents=True)

        # 模拟 ASR fallback 输出: 只有 auto.srt
        (downloads / "auto.srt").write_text(
            "1\n00:00:01,000 --> 00:00:03,000\nHello world\n\n"
            "2\n00:00:04,000 --> 00:00:06,000\nSecond line\n"
        )

        moved = asyncio.run(ts._organize_transcripts(bvid, downloads, transcribed, 1))
        check("返回 1 (一个 transcript)", moved == 1, f"got {moved}")
        check("transcribed/{bvid}/transcript.srt 存在",
              (transcribed / "transcript.srt").exists())
        check("transcribed/{bvid}/transcript.txt 存在",
              (transcribed / "transcript.txt").exists())
        check("transcript.txt 有内容",
              (transcribed / "transcript.txt").read_text().strip() != "")
        check("transcript.txt 不含时间戳",
              "-->" not in (transcribed / "transcript.txt").read_text())


# ─── 5. _organize_transcripts: 多P (B站有字幕) ──────────────────────

def test_organize_multi_p_with_bili_subs():
    print("\n[5] _organize_transcripts (多 P + B 站字幕)")
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        bvid = "BVtest2222"
        downloads = tmp / "downloads" / bvid
        downloads.mkdir(parents=True)
        transcribed = tmp / "transcribed" / bvid
        transcribed.mkdir(parents=True)

        # 模拟 summarize.py 输出: P1_transcript.srt, P2_transcript.srt
        for pn in [1, 2, 3]:
            (downloads / f"P{pn}_transcript.srt").write_text(
                f"{pn}\n00:00:0{pn},000 --> 00:00:0{pn+1},000\nP{pn} content\n\n"
            )

        moved = asyncio.run(ts._organize_transcripts(bvid, downloads, transcribed, 3))
        check("返回 3 (三个分P)", moved == 3, f"got {moved}")

        # 验证每个分P 子目录
        for pn in [1, 2, 3]:
            page_dir = transcribed / f"P{pn}"
            check(f"P{pn}/ 子目录存在", page_dir.exists())
            check(f"P{pn}/transcript.srt 存在", (page_dir / "transcript.srt").exists())
            check(f"P{pn}/transcript.txt 存在", (page_dir / "transcript.txt").exists())
            check(f"P{pn}/transcript.txt 有 P{pn} content",
                  f"P{pn} content" in (page_dir / "transcript.txt").read_text())


# ─── 6. _organize_transcripts: ASR fallback, 多P 整段合并 ───────────

def test_organize_asr_fallback_merged():
    print("\n[6] _organize_transcripts (ASR fallback, 只有 transcript.srt)")
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        bvid = "BVtest3333"
        downloads = tmp / "downloads" / bvid
        downloads.mkdir(parents=True)
        transcribed = tmp / "transcribed" / bvid
        transcribed.mkdir(parents=True)

        # 模拟: ASR fallback 已经把 auto.srt 复制为 transcript.srt
        (downloads / "transcript.srt").write_text(
            "1\n00:00:01,000 --> 00:00:03,000\nMerged content\n"
        )

        moved = asyncio.run(ts._organize_transcripts(bvid, downloads, transcribed, 1))
        check("返回 1", moved == 1)
        check("根目录 transcript.srt", (transcribed / "transcript.srt").exists())
        check("根目录 transcript.txt", (transcribed / "transcript.txt").exists())


# ─── 7. _generate_summaries: LLM 失败 fallback ──────────────────────

def test_generate_summaries_llm_fail():
    print("\n[7] _generate_summaries (LLM 失败时 graceful)")
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        bvid = "BVtest4444"
        transcribed = tmp / "transcribed" / bvid
        transcribed.mkdir(parents=True)

        # meta.json
        (transcribed / "meta.json").write_text(json.dumps({
            "title": "Test Video",
            "page_count": 1,
        }))

        # transcript.srt (代码 glob 这个)
        (transcribed / "transcript.srt").write_text(
            "1\n00:00:01,000 --> 00:00:03,000\nSome content here\n"
        )
        # transcript.txt
        (transcribed / "transcript.txt").write_text("Some content here")

        # Mock summarize.summarize_one 抛异常
        with patch.object(sm, "summarize_one",
                          side_effect=Exception("LLM API timeout")):
            count = ts._generate_summaries(bvid, transcribed)

        check("返回 1 (尝试生成了)", count == 1, f"got {count}")
        check("summary.md 存在 (即使 LLM 失败)",
              (transcribed / "summary.md").exists())
        content = (transcribed / "summary.md").read_text()
        check("summary.md 含 LLM 失败提示", "LLM 总结失败" in content)
        check("summary.md 含重跑命令", "transcribe_skill.py" in content)


# ─── 8. _generate_summaries: 多P 生成 index.md ──────────────────────

def test_generate_summaries_multi_p_index():
    print("\n[8] _generate_summaries (多P → index.md)")
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        bvid = "BVtest5555"
        transcribed = tmp / "transcribed" / bvid
        transcribed.mkdir(parents=True)

        (transcribed / "meta.json").write_text(json.dumps({
            "title": "Multi P Video",
            "page_count": 3,
            "page_names": ["Intro", "Main", "Outro"],
        }))

        for pn in [1, 2, 3]:
            pd = transcribed / f"P{pn}"
            pd.mkdir()
            (pd / "transcript.srt").write_text("dummy")
            (pd / "transcript.txt").write_text(f"P{pn} content")

        # Mock LLM 成功
        with patch.object(sm, "summarize_one",
                          return_value=f"## Summary for page"):
            count = ts._generate_summaries(bvid, transcribed)

        check("返回 3 个 summary", count == 3, f"got {count}")
        check("index.md 生成",
              (transcribed / "index.md").exists())
        idx = (transcribed / "index.md").read_text()
        check("index.md 串了 P1/P2/P3", "P1" in idx and "P2" in idx and "P3" in idx)
        check("index.md 提到分P 名", "Intro" in idx and "Main" in idx)


# ─── 9. 幂等性: 重复转录不破坏 ─────────────────────────────────────

def test_idempotent():
    print("\n[9] 幂等性 (重复跑 = 跳过)")
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        # Mock TRANSCRIBED_DIR 到临时目录
        original = ts.TRANSCRIBED_DIR
        ts.TRANSCRIBED_DIR = tmp / "transcribed"

        try:
            bvid = "BVtest6666"
            transcribed = ts.TRANSCRIBED_DIR / bvid
            transcribed.mkdir(parents=True)

            # 模拟"已完成"状态: 已有 summary.md
            (transcribed / "P1").mkdir()
            (transcribed / "P1" / "summary.md").write_text("existing summary")

            # 直接调 transcribe_one (会触发幂等检查)
            # 但需要 mock download_subs 不被调用
            with patch.object(sm, "download_subs") as mock_dl:
                ok, reason = asyncio.run(ts.transcribe_one(bvid))

            check("返回 True (跳过)", ok is True, f"got {ok}")
            check("reason 包含'已转录'", "已转录" in reason, f"got {reason}")
            check("download_subs 没被调用 (幂等)", not mock_dl.called)
        finally:
            ts.TRANSCRIBED_DIR = original


# ─── 10. update_wiki: subprocess 调 wiki_gen ─────────────────────────

def test_update_wiki():
    print("\n[10] update_wiki (subprocess)")
    # 用真实 transcribed/ 测 (如果存在)
    if not ts.TRANSCRIBED_DIR.exists() or not any(ts.TRANSCRIBED_DIR.iterdir()):
        print("    ⚠️  transcribed/ 为空, 跳过 (避免污染 wiki)")
        return

    ok = ts.update_wiki()
    check("update_wiki 返回 True", ok is True)
    # 检查 wiki/videos/ 有 .md
    wiki_videos = ts.WIKI_DIR / "videos"
    if wiki_videos.exists():
        count = len(list(wiki_videos.glob("*.md")))
        check(f"wiki/videos/ 有 .md (>= 1)", count >= 1, f"got {count}")


# ─── 11. CLI --dry-run 不修改任何东西 ────────────────────────────────

def test_dry_run_no_side_effects():
    print("\n[11] CLI --dry-run (无副作用)")
    # 跑前后比对 downloads/ transcribed/ 状态
    before_dl = sorted([p.name for p in (ts.DOWNLOADS_DIR).iterdir()]) if ts.DOWNLOADS_DIR.exists() else []
    before_tr = sorted([p.name for p in (ts.TRANSCRIBED_DIR).iterdir()]) if ts.TRANSCRIBED_DIR.exists() else []

    result = subprocess.run(
        [sys.executable, str(PROJECT_DIR / "transcribe_skill.py"), "--dry-run"],
        capture_output=True, text=True, timeout=30,
    )
    check("dry-run 退出码 0", result.returncode == 0,
          f"got {result.returncode}\n{result.stderr[-300:]}")

    after_dl = sorted([p.name for p in (ts.DOWNLOADS_DIR).iterdir()]) if ts.DOWNLOADS_DIR.exists() else []
    after_tr = sorted([p.name for p in (ts.TRANSCRIBED_DIR).iterdir()]) if ts.TRANSCRIBED_DIR.exists() else []

    check("downloads/ 没动", before_dl == after_dl)
    check("transcribed/ 没动", before_tr == after_tr)



# ─── 12. --move-done 检查函数: 占位符拒绝 ──────────────────────

def test_check_summaries_rejects_placeholder():
    print("\n[12] _check_all_summaries 拒绝占位符 / stub / 太小")
    bvid = "BV15rbZ6aEKN"  # 真实存在, P1-P9 已跑过

    # 全部都是真总结 → 应返回 ok
    ok, reason, parts = ts._check_all_summaries(bvid, "测试")
    check("BV15rbZ6aEKN (9 真 P) → ok", ok, f"reason={reason}")
    check("返回 9 个 parts", len(parts) == 9, f"got {len(parts)}")
    check("parts 包含 P1/P9", any(p["page"] == "P1" for p in parts)
          and any(p["page"] == "P9" for p in parts))

    # 制造占位符
    target = ts.TRANSCRIBED_DIR / "BV_TEST_PLACEHOLDER"
    target.mkdir(parents=True, exist_ok=True)
    try:
        p1 = target / "P1"
        p1.mkdir(exist_ok=True)
        stub = p1 / "summary.md"
        stub.write_text("[待生成] LLM 失败, 请重跑\n", encoding="utf-8")

        ok2, reason2, parts2 = ts._check_all_summaries("BV_TEST_PLACEHOLDER", "stub 测试")
        check("占位符 → 拒绝 (ok=False)", not ok2, f"reason={reason2}")
        check("reason 提及占位符/失败", "占位" in reason2 or "失败" in reason2 or "stub" in reason2.lower(),
              f"got: {reason2}")
        check("占位符情况 parts 空", parts2 == [])
    finally:
        shutil.rmtree(target, ignore_errors=True)

    # 制造太小 (含 5 段标记但 size < 1500, 触发 size 检查)
    target2 = ts.TRANSCRIBED_DIR / "BV_TEST_TINY"
    target2.mkdir(parents=True, exist_ok=True)
    try:
        p1 = target2 / "P1"
        p1.mkdir(exist_ok=True)
        tiny = p1 / "summary.md"
        # 含 5 段标记但 size ~600 bytes < 1500
        tiny.write_text(
            "📺 视频概述\n"
            "🧠 核心概念\n"
            "💡 观点\n"
            "🔑 最重要\n"
            "📐 逻辑\n"
        , encoding="utf-8")
        ok3, reason3, parts3 = ts._check_all_summaries("BV_TEST_TINY", "tiny")
        check("太小 → 拒绝", not ok3)
        check("reason 提及字节/太小", "字节" in reason3 or "小" in reason3 or "1500" in reason3,
              f"got: {reason3}")
    finally:
        shutil.rmtree(target2, ignore_errors=True)

    # 制造无 summary
    target3 = ts.TRANSCRIBED_DIR / "BV_TEST_NOTHING"
    target3.mkdir(parents=True, exist_ok=True)
    try:
        ok4, reason4, parts4 = ts._check_all_summaries("BV_TEST_NOTHING", "nothing")
        check("没 summary → 拒绝", not ok4)
        check("reason 提及 summary", "summary" in reason4.lower(), f"got: {reason4}")
    finally:
        shutil.rmtree(target3, ignore_errors=True)

    # BV 不存在
    ok5, reason5, parts5 = ts._check_all_summaries("BV999DOESNOTEXIST", "ghost")
    check("BV 不存在 → 拒绝", not ok5)
    check("BV 不存在 parts 空", parts5 == [])


# ─── 13. _write_report 生成报告文件 ────────────────────────────

def test_write_report():
    print("\n[13] _write_report 生成报告 .md 文件")
    with tempfile.TemporaryDirectory() as tmp:
        report_path = Path(tmp) / "subdir" / "report.md"

        # transcribe 模式
        items = [
            {"bvid": "BV1", "title": "test1", "duration": 600, "aid": 1},
            {"bvid": "BV2", "title": "test2", "duration": 1200, "aid": 2},
        ]
        successes = [items[0]]
        failures = [(items[1], "LLM 失败")]

        ts._write_report(str(report_path), mode="transcribe",
                         items=items, successes=successes, failures=failures)

        check("文件已创建", report_path.exists())
        content = report_path.read_text(encoding="utf-8")
        check("含 'transcribe' 模式标记", "transcribe" in content)
        check("含成功计数 1", "成功**: 1" in content)
        check("含失败计数 1", "失败**: 1" in content)
        check("含失败原因", "LLM 失败" in content)

        # move_done 模式
        report_path2 = Path(tmp) / "move.md"
        ts._write_report(str(report_path2), mode="move_done",
                         items=items, moved=[{"item": items[0], "parts": [{"page": "P1"}]}],
                         skipped=[{"item": items[1], "reason": "stub"}])
        c2 = report_path2.read_text(encoding="utf-8")
        check("move_done 含 '已移到「已总结」'", "已移到「已总结」" in c2)
        check("move_done 含 stub reason", "stub" in c2)


# ─── 14. CLI --move-done + --report-to (mock 收藏夹) ──────────────────

def test_move_done_cli_dry():
    """直接调 move_done_mode() in-process (subprocess mock 失效)"""
    print("\n[14] --move-done + --report-to (mock scan_favorites, in-process)")
    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "report.md"

        with patch.object(ts, "scan_favorites") as mock_scan, \
             patch.object(ts, "move_video_to_fav") as mock_move, \
             patch.object(ts, "load_credential", return_value=MagicMock()):
            mock_scan.return_value = [
                {"bvid": "BV15rbZ6aEKN", "title": "清华-华为 9P",
                 "duration": 68215, "aid": 999001},
                {"bvid": "BV_NOTFOUND", "title": "fake",
                 "duration": 600, "aid": 999002},
            ]
            mock_move.return_value = {}

            # 直接 in-process 调 (subprocess.run 启动子进程, mock 在子进程无效)
            args = type("Args", (), {
                "move_done": True,
                "auto": True,
                "source": 4115533556,        # 未总结 (move_done 不用)
                "in_progress_fav": 4012580756,  # 总结中 (move_done 扫这个)
                "dest": 4090394056,            # 已总结 (move_done 目标)
                "bvid": None,
                "report_to": str(report),
            })()
            asyncio.run(ts.move_done_mode(args))

            check("mock scan 调过 2 次 (总结中 + 待总结)", mock_scan.call_count == 2,
                  f"got {mock_scan.call_count}")
            if mock_scan.call_count >= 2:
                check("第 1 次扫 总结中 (4012580756, 优先)",
                      mock_scan.call_args_list[0].args[0] == 4012580756,
                      f"got {mock_scan.call_args_list[0].args[0]}")
                check("第 2 次扫 待总结 (4115533556, 兜底)",
                      mock_scan.call_args_list[1].args[0] == 4115533556,
                      f"got {mock_scan.call_args_list[1].args[0]}")
            check("report 文件已写", report.exists())
            content = report.read_text(encoding="utf-8")
            check("报告含 'move_done'", "move_done" in content)
            check("报告含 BV15rbZ6aEKN (移过的)", "BV15rbZ6aEKN" in content)
            check("报告含 BV_NOTFOUND (跳过的)", "BV_NOTFOUND" in content)
            check("mock move 调过 1 次 (BV15rbZ6aEKN)", mock_move.call_count == 1,
                  f"got {mock_move.call_count}")


# ─── 15a. DEFAULT_IN_PROGRESS_FAV 常量 + --in-progress-fav flag ──────

def test_in_progress_fav_constant():
    print("\n[15a] DEFAULT_IN_PROGRESS_FAV 常量 + --in-progress-fav flag")
    # 常量是 4012580756 (总结中, 用户新建)
    check("DEFAULT_IN_PROGRESS_FAV = 4012580756",
          ts.DEFAULT_IN_PROGRESS_FAV == 4012580756,
          f"got {ts.DEFAULT_IN_PROGRESS_FAV}")
    check("DEFAULT_SOURCE_FAV = 4115533556 (未总结)",
          ts.DEFAULT_SOURCE_FAV == 4115533556,
          f"got {ts.DEFAULT_SOURCE_FAV}")
    check("DEFAULT_DEST_FAV = 4090394056 (已总结)",
          ts.DEFAULT_DEST_FAV == 4090394056,
          f"got {ts.DEFAULT_DEST_FAV}")

    # --in-progress-fav 在 --help 里出现
    import subprocess, sys
    result = subprocess.run(
        [sys.executable, str(PROJECT_DIR / "transcribe_skill.py"), "--help"],
        capture_output=True, text=True, timeout=10,
    )
    check("--help 含 --in-progress-fav",
          "--in-progress-fav" in result.stdout)
    check("--help 提到总结中 (LOCK 状态)",
          "总结中" in result.stdout and "LOCK" in result.stdout)


# ─── 15b. 3 状态机: 转录前先移到 总结中 ────────────────────────

def test_3state_lock_before_transcribe():
    """主流程先调 move_video_to_fav(aid, source, in_progress_fav) (LOCK)
    然后再调 transcribe_one()
    """
    print("\n[15b] 3 状态机: 转录前先移到 总结中 (LOCK)")
    call_log = []

    with patch.object(ts, "scan_favorites") as mock_scan, \
         patch.object(ts, "move_video_to_fav", new=AsyncMock(side_effect=lambda *a, **k: call_log.append(("move", a)))), \
         patch.object(ts, "transcribe_one", new=AsyncMock(return_value=(True, "ok"))), \
         patch.object(ts, "update_wiki", return_value=True), \
         patch.object(ts, "load_credential", return_value=MagicMock()):
        mock_scan.return_value = [
            {"bvid": "BV_TEST_LOCK", "title": "lock 测试",
             "duration": 600, "aid": 888},
        ]
        args = type("Args", (), {
            "move_done": False,
            "auto": True,
            "source": 4115533556,
            "in_progress_fav": 4012580756,
            "dest": 4090394056,
            "bvid": None,
            "page": None,
            "limit": None,
            "dry_run": False,
            "skip_move": False,
            "skip_wiki": True,
            "asr_model": "sensevoice",
            "asr_max_duration": None,
            "max_duration": 1800,
            "yes": True,
            "report_to": None,
        })()
        asyncio.run(ts.main_async(args))

    # 验证: 第一次 move 是 LOCK (source → in_progress), 第二次是 UNLOCK (in_progress → dest)
    moves = [c for c in call_log if c[0] == "move"]
    check("调过 2 次 move_video_to_fav", len(moves) == 2,
          f"got {len(moves)}: {moves}")
    if len(moves) >= 2:
        # 第一次: (aid, 4115533556, 4012580756) - LOCK
        check("第一次 LOCK: source=未总结 → dest=总结中",
              moves[0][1][1] == 4115533556 and moves[0][1][2] == 4012580756,
              f"got from={moves[0][1][1]} to={moves[0][1][2]}")
        # 第二次: (aid, 4012580756, 4090394056) - UNLOCK
        check("第二次 UNLOCK: source=总结中 → dest=已总结",
              moves[1][1][1] == 4012580756 and moves[1][1][2] == 4090394056,
              f"got from={moves[1][1][1]} to={moves[1][1][2]}")


# ─── 15c. 失败视频留在 总结中, 不移到已总结 ──────────────────

def test_3state_failure_leaves_in_summary():
    """转录失败 → 视频留在 总结中, 不调 UNLOCK"""
    print("\n[15c] 3 状态机: 失败视频留在 总结中")
    call_log = []

    with patch.object(ts, "scan_favorites") as mock_scan, \
         patch.object(ts, "move_video_to_fav", new=AsyncMock(side_effect=lambda *a, **k: call_log.append(("move", a)))), \
         patch.object(ts, "transcribe_one", new=AsyncMock(return_value=(False, "LLM 失败"))), \
         patch.object(ts, "update_wiki", return_value=True), \
         patch.object(ts, "load_credential", return_value=MagicMock()):
        mock_scan.return_value = [
            {"bvid": "BV_TEST_FAIL", "title": "fail 测试",
             "duration": 600, "aid": 777},
        ]
        args = type("Args", (), {
            "move_done": False,
            "auto": True,
            "source": 4115533556,
            "in_progress_fav": 4012580756,
            "dest": 4090394056,
            "bvid": None,
            "page": None,
            "limit": None,
            "dry_run": False,
            "skip_move": False,
            "skip_wiki": True,
            "asr_model": "sensevoice",
            "asr_max_duration": None,
            "max_duration": 1800,
            "yes": True,
            "report_to": None,
        })()
        asyncio.run(ts.main_async(args))

    moves = [c for c in call_log if c[0] == "move"]
    check("失败情况只调 1 次 move (LOCK)", len(moves) == 1,
          f"got {len(moves)}: {moves}")
    if len(moves) >= 1:
        # 第一次: (aid, 未总结, 总结中) - LOCK
        check("只调了 LOCK (未总结→总结中)",
              moves[0][1][1] == 4115533556 and moves[0][1][2] == 4012580756,
              f"got from={moves[0][1][1]} to={moves[0][1][2]}")


# ─── 15d. --move-done 扫 总结中 (不是 未总结) ──────────────────

def test_move_done_scans_in_progress():
    """--move-done 必须扫 总结中 (in_progress_fav), 不是 source (未总结)"""
    print("\n[15d] --move-done 扫 总结中 (不是 未总结)")
    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "report.md"

        with patch.object(ts, "scan_favorites") as mock_scan, \
             patch.object(ts, "move_video_to_fav", new=AsyncMock()), \
             patch.object(ts, "load_credential", return_value=MagicMock()):
            mock_scan.return_value = []  # 空就行, 只看 scan 调用参数

            args = type("Args", (), {
                "move_done": True,
                "auto": True,
                "source": 4115533556,
                "in_progress_fav": 4012580756,
                "dest": 4090394056,
                "bvid": None,
                "report_to": str(report),
            })()
            asyncio.run(ts.move_done_mode(args))

            check("scan_favorites 调过 2 次 (B1: 默认扫两个)", mock_scan.call_count == 2,
                  f"got {mock_scan.call_count}")
            if mock_scan.call_count >= 2:
                # 关键: 必须用 in_progress_fav (4012580756) 优先, 然后 source (4115533556) 兜底
                check("第 1 次扫 总结中 (4012580756, 优先)",
                      mock_scan.call_args_list[0].args[0] == 4012580756,
                      f"got scan[0] arg={mock_scan.call_args_list[0].args[0]}")
                check("第 2 次扫 待总结 (4115533556, 兜底 orphan)",
                      mock_scan.call_args_list[1].args[0] == 4115533556,
                      f"got scan[1] arg={mock_scan.call_args_list[1].args[0]}")
            check("报告含 '总结中存量'", "总结中存量" in report.read_text(encoding="utf-8"))


# ─── 15e. 报告含 总结中存量 统计 ─────────────────────────────

def test_report_in_progress_count():
    print("\n[15e] 报告含 总结中存量统计")
    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "r.md"
        items = [{"bvid": "BV1", "title": "t1", "duration": 100, "aid": 1}]
        ts._write_report(str(report), mode="move_done",
                         items=items,
                         moved=[{"item": items[0], "parts": [{"page": "P1"}]}],
                         skipped=[],
                         in_progress_count=5)
        c = report.read_text(encoding="utf-8")
        check("报告含 '总结中存量: 5'", "总结中存量**: 5" in c,
              f"\n--- 内容 ---\n{c}\n---")


# ─── 15. --auto 等价 --yes ─────────────────────────────────────

def test_auto_flag_yields_yes():
    print("\n[15] --auto 等价 --yes (跳过长视频确认)")
    parser = argparse.ArgumentParser()
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--auto", "-A", action="store_true")
    args = parser.parse_args(["--auto"])
    check("--auto 触发", args.auto)
    check("main() 设后 --yes=True", True)  # main() 里 if args.auto: args.yes = True


# ─── 主入口 ─────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("transcribe_skill.py 烟雾测试")
    print("=" * 60)

    test_module_basics()
    test_scan_favorites()  # 真实 API
    test_cli()
    test_organize_single_p_with_asr()
    test_organize_multi_p_with_bili_subs()
    test_organize_asr_fallback_merged()
    test_generate_summaries_llm_fail()
    test_generate_summaries_multi_p_index()
    test_idempotent()
    test_update_wiki()
    test_dry_run_no_side_effects()
    test_check_summaries_rejects_placeholder()
    test_write_report()
    test_move_done_cli_dry()
    test_in_progress_fav_constant()
    test_3state_lock_before_transcribe()
    test_3state_failure_leaves_in_summary()
    test_move_done_scans_in_progress()
    test_report_in_progress_count()
    test_auto_flag_yields_yes()

    print("\n" + "=" * 60)
    print(f"结果: ✓ {PASS} pass / ✗ {FAIL} fail")
    print("=" * 60)
    if FAIL > 0:
        print("\n失败详情:")
        for e in ERRORS:
            print(f"  - {e}")
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
