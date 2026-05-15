# captcha_solver

独立的验证码识别模块。

当前实现聚焦腾讯点选验证码：

- `captcha_solver.image`：纯图片算法，不依赖 Selenium、95598 或业务数据。
- `captcha_solver.tencent`：腾讯验证码 DOM 适配层，负责截图、点击、刷新和报告保存。
- `captcha_solver.tools.replay_point_click`：离线回放保存下来的点选验证码样本。

业务项目只需要调用 `TencentCaptchaHandler`。算法迭代应优先使用 `data/pages/` 里的样本离线 replay，避免频繁线上登录触发风控。

```bash
python3 -m captcha_solver.tools.replay_point_click --summary-only
```

在线失败或低置信时会生成：

- `tencent_point_click_answer_*.png`
- `tencent_point_click_bg_*.png`
- `tencent_point_click_report_*.json`

这些文件用于复盘候选区域、Top 分数、拒绝原因和最终点位。
