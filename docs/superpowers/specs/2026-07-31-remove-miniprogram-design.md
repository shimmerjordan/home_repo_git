# 移除微信小程序 — 设计

日期: 2026-07-31
状态: 待实施

## 背景

微信小程序作为"独立第三端"的路线被判定走不通,不再维护。仓库里保留它只会让文档导航、
备份逻辑和目录结构持续背负一条死路径。

## 目标

从仓库中彻底移除小程序的代码、文档、导航入口与专为它服务的后端逻辑。

## 覆盖面盘点

全仓 grep(排除 `miniprogram/`、`frontend/dist/`、`.git/`、`node_modules/`)命中
`miniprogram|小程序|微信|wechat|weixin` 的文件只有 6 个,覆盖面干净。

**分支情况**:`git branch -a` 只有 `main` 与 `remotes/origin/main`,**不存在小程序分支**,
无分支可删。

## 删除

| 路径 | 说明 |
|---|---|
| `miniprogram/` | 整个目录,40 个文件 244K |
| `docs/wechat-miniprogram.md` | 可行性调研 + 架构 + 进度 |
| `docs/deployment-miniprogram.md` | 本地运行 + 上架 + 数据互通 |

## 修改

### README.md

删除文档导航表的三行([README.md:34-36](../../../README.md#L34-L36)):
`wechat-miniprogram`、`deployment-miniprogram`、`miniprogram/`。

### docs/backup.md

[第 18 行](../../../docs/backup.md#L18)去掉"跨端 (如小程序) 导入"的措辞,改为说明 JSON 是
可读导出,供人工查看。

### backend/app/services/backup.py

这里有**真实功能代码**,不只是注释:

1. **删除 `_restore_database_from_json` 函数**([backup.py:488](../../../backend/app/services/backup.py#L488) 起)
   —— 当初为"小程序备份没有裸 SQLite"而写。
2. **删除 `restore` 里的 `elif` 分支**([backup.py:456-459](../../../backend/app/services/backup.py#L456-L459))。
3. **改模块 docstring 第 5 行**的"跨端 (小程序) 导入"措辞。

#### 已知后果与处理

裸库 `db/storage.db` 只在勾选 `inventory` 组件、且库文件存在时才入包
([backup.py:93-96](../../../backend/app/services/backup.py#L93-L96))。因此**只勾选「操作流水」
或「审计日志」的 NAS 备份包本身就没有裸库** —— 这类包并非小程序独有。删掉 JSON 重建路径后,
对它们执行 `targets=["database"]` 恢复将无事发生。

若原样删掉 `elif` 分支,`restored` 数组里不含 `"database"`,用户容易误以为恢复成功。
因此**把该分支替换为显式报错**,而不是静默跳过:

```python
if "database" in targets:
    if "db/storage.db" in names:
        _restore_database(zf.read("db/storage.db"))
        restored.append("database")
    else:
        raise ValueError("备份包内没有裸 SQLite 快照 (db/storage.db), 无法恢复数据库。"
                         "该包可能只勾选了流水或审计组件。")
```

`ValueError` 是正确选择:路由层
[routers/backup.py:119-120](../../../backend/app/routers/backup.py#L119-L120) 与
[137-138](../../../backend/app/routers/backup.py#L137-L138) 已捕获 `(ValueError, RuntimeError)`
并转成 HTTP 400 + 原始消息,前端备份面板会直接显示这段中文提示,无需额外改动。

**JSON 导出本身保留**:`data/*.json` 继续入包。它对人工查看备份内容仍有价值,
且删掉会牵连备份选择性组件与文档,超出本次范围。

## 明确不动

| 位置 | 原因 |
|---|---|
| [docs/bots/README.md:20-22](../../../docs/bots/README.md#L20-L22) 的「企业微信应用」「微信个人号」 | 这是**群机器人接入对比表**,与小程序无关,是有效的技术选型记录 |
| git 历史(`bc80aea wx miniapp`、`e32ed4b 移除同声传译插件` 等) | 已推送到 `origin/main`,重写历史会破坏远端并影响任何已有克隆。移除作为一次新提交完成 |
| `docs/changelog.md`、`docs/architecture.md` | grep 无命中,不含小程序内容 |
| `docker-compose.yml`、`start.sh`、`.gitignore` | grep 无命中,小程序从未进入部署链路 |

## 验证

1. `grep -rIn --exclude-dir=.git --exclude-dir=node_modules --exclude-dir=dist -iE "miniprogram|小程序" .`
   —— 应只剩 `docs/superpowers/specs/` 下的本文件与快捷操作 spec 的交叉引用
2. 后端能正常导入启动:`cd backend && python -c "from app.main import app"`
3. 备份面板手工验证:
   - 勾选全部组件跑一次备份 → 恢复 database 成功(走裸库主道)
   - 只勾选「操作流水」跑一次备份 → 恢复 database 时**看到明确错误提示**,而非静默无事
4. README 文档导航表里不再出现小程序三行,其余链接可点

## 改动文件

| 文件 | 改动 |
|---|---|
| `miniprogram/` | 删除整个目录 |
| `docs/wechat-miniprogram.md` | 删除 |
| `docs/deployment-miniprogram.md` | 删除 |
| `README.md` | 删 3 行导航 |
| `docs/backup.md` | 改 1 行措辞 |
| `backend/app/services/backup.py` | 删函数 + 改分支为显式报错 + 改 2 处注释 |
