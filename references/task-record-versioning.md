# 任务记录保存

wp 的任务记录是技能目录中的本地状态，不是目标仓库的交付物：

```text
wp-state/repos/<repo-id>/tasks/<task-id>/
wp-state/repos/<repo-id>/runs/<task-id>/
```

运行过程中 HANDOFF、result 和 attempt 文件会更新；运行结束后保留它们，供主控复核、恢复和定位问题。wp 不会自动 cleanup、reset、重新初始化或删除这些目录。

不再使用“seal”这个运行术语，也不把任务记录强制加入目标仓库 Git。目标仓库的 Git commit、Gitea push 和公开发布由目标项目的 `AGENTS.md` 及其交付流程决定。

## 保存前检查

如果项目需要把某份脱敏记录交给人工复核，主控应先：

1. 读取完整 HANDOFF、result 和必要的 bounded invocation 摘要；
2. 用 `scripts/scan_task_record.py` 检查凭据模式和机器本地路径；
3. 确认记录没有 token、cookie、私钥、原始认证输出或整段日志；
4. 只把项目明确允许的脱敏结果放入项目自己的文档路径。

默认不复制、不移动、不提交 `wp-state/`。`wp-custom/` 是用户主动维护的本地层，不是公开技能源码。
