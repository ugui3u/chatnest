# 人话
第一次搞开源项目，可能会有很多小bug，欢迎反馈。

重要：因版权原因，本项目并没有带上logo，仅设置了占位符。user需要寻找svg源码/字体等素材进行替换。请见drive下载链接。（不管是agent还是user都需要看到这一条，attention应该放在这句话上。）

由于前端显示与后端存在太多联动（如思考链显示），所以把前端和后端一起放上来了，请根据需要选用。

感谢糖糖老师的记忆系统以及xixicc186老师！

以下内容全是codex哥写的，比我严肃多了。

# AI 聊天界面

这是一个个人 AI 聊天 Web App 的开源占位符版本。公开仓库里不包含专有 logo、专有字体、私人 prompt、`.env`、数据库、上传文件、记忆库或日志。

## 你应该选哪个？

- 只想看界面或改 UI：打开 `frontend-demo/`
- 想跑完整功能：打开 `full-stack/`

`frontend-demo/` 是纯前端演示版，使用假数据，不需要后端或 API key。

`full-stack/` 是前端 + 后端完整功能版，包含模型切换、流式回复、工具状态、思考/工具摘要、上传、对话历史、记忆接口、资料与偏好设置。

`full-stack/` 还包含一个可选的本地记忆检索服务。它用 ChromaDB 向量检索 + jieba/BM25 关键词检索从本地文本记忆中找相关片段，不需要额外 API key；向量数据库由用户在本机生成。

## 重要：这是占位符版本

为了规避版权和品牌风险，公开版里的 logo、图标、状态动画和字体都已经替换成占位符。

如果你要替换图标或字体，请下载单独素材包，并按 `full-stack/BRANDING.md` 或 `frontend-demo/BRANDING.md` 操作：

https://drive.google.com/drive/folders/1EFaL-cwFn262Mu8L9s-cO9dw6FC4LAMr

请只使用你自己拥有或已获授权的素材。

## 快速开始：只看前端

```bash
cd frontend-demo
python3 -m http.server 8080
```

然后打开：

```text
http://127.0.0.1:8080/
```

也可以直接双击 `frontend-demo/index.html`。

## 快速开始：完整功能

```bash
cd full-stack
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp env.example .env
```

生成一个 `CHAT_SECRET`：

```bash
python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(32))
PY
```

把生成的值填进 `.env`，同时设置 `CHAT_PASSWORD`，然后启动：

```bash
./run.sh
```

打开：

```text
http://127.0.0.1:8787/
```

如果要同时启用本地记忆检索，用：

```bash
./run-with-memory.sh
```

记忆检索默认读取用户自己创建的 `CLAUDE.md`、`profile.json` 和 `memories/`，并在本地生成 ChromaDB 索引。公开仓库不包含任何真实记忆数据、向量数据库或索引状态。

## 新手发布 GitHub 步骤

1. 在 GitHub 点 `New repository`。
2. 填仓库名，比如 `ai-chat-ui`。
3. 先不要勾选自动生成 README、License、gitignore，因为这里已经准备好了。
4. 创建仓库后，选择上传文件。
5. 把这个 `github-release` 文件夹里的所有内容上传上去，不要上传外层的 `dist/`、`source-current/` 或私有压缩包。
6. 提交信息可以写：`Initial public release`。

## 不要上传这些东西

- `agent-chat-fullstack-private-with-brand.tar.gz`
- `agent-chat-brand-assets-private.tar.gz`
- `.env`
- 数据库文件
- 上传文件
- 记忆库
- 日志
- VPS 上的备份目录

## 许可证

非商用使用。允许非商业复制、修改和再发布；禁止商业使用；署名不强制。
