# 探路脚本 (Explore Scripts)

为 MikuNotes v0.3.0 的 6 个新功能在 Python 端做 API 和算法的可行性探查。

## 9 个探路脚本

### A. B 站 API 探路（4 个）

| 脚本 | API | 用途 |
|------|-----|------|
| `explore_01_fav_folders.py` | `/x/v3/fav/folder/created/list-all` | 列出所有收藏夹 |
| `explore_02_fav_videos.py` | `/x/v3/fav/resource/list` | 拿收藏夹内视频（分页） |
| `explore_03_followings.py` | `/x/relation/followings` | 拿关注列表（UP 主） |
| `explore_04_up_videos.py` | `/x/space/wbi/arc/search` | 拿 UP 主投稿（WBI 签名） |

### B. 算法探路（5 个）

| 脚本 | 算法 | 用途 |
|------|------|------|
| `explore_05_tag_extraction.py` | LLM 提取 tags | 给视频打标签 |
| `explore_06_cross_video.py` | LLM 多视频聚合 | 跨视频洞察 |
| `explore_07_long_video.py` | LLM 切片再总结 | 超长视频分话题 |
| `explore_08_tag_cluster.py` | Union-Find / Jaccard | 按 tag 聚类 |
| `explore_09_incremental_import.py` | 分页 + 跳过 | 增量导入 |

## 使用方法

### 前置条件

1. **登录 B 站** (一次性):
   ```bash
   cd /Users/fundou/my_proj/bilibili-summarizer
   python3 bilibili_cc.py --login
   ```

2. **设置 LLM API Key** (用于 #5/#6/#7):
   ```bash
   export DEEPSEEK_API_KEY=sk-xxx
   # 或
   export MINIMAX_API_KEY=xxx
   ```

### 跑单个

```bash
cd /Users/fundou/my_proj/bilibili-summarizer/explore

# B 站 API (不需要 LLM key)
python3 explore_01_fav_folders.py
python3 explore_02_fav_videos.py
python3 explore_03_followings.py
python3 explore_04_up_videos.py

# 算法 (需要 LLM key)
python3 explore_05_tag_extraction.py
python3 explore_06_cross_video.py
python3 explore_07_long_video.py

# 不需要 B 站登录
python3 explore_08_tag_cluster.py
python3 explore_09_incremental_import.py
```

## 探路 → 实现的映射

每个脚本跑通后，**逻辑会搬到 Flutter App**:

| 探路脚本 | 对应 Flutter 模块 |
|---------|------------------|
| #1-4 | `lib/core/bilibili/` 新增 endpoints |
| #5-6 | `lib/core/llm/` 新增 template types |
| #7 | `lib/core/llm/long_video_processor.dart` |
| #8 | `lib/core/storage/cluster.dart` |
| #9 | `lib/ui/screens/home/import_screen.dart` |

## 状态

- [x] 9 个探路脚本写完
- [ ] 等用户登录 B 站后跑 #1-4
- [ ] 等用户设置 LLM Key 后跑 #5-7
- [ ] #8 纯算法,已可跑
- [ ] #9 需要 B 站登录
