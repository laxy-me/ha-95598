# Home Assistant Add-on: 95598 for Home Assistant

这个 add-on 用于在 Home Assistant OS / Supervised 中运行 `ha-95598`，通过 MQTT Discovery 把国家电网 `95598` 的电量、电费、余额和历史数据同步到 Home Assistant。

## 使用前准备

- 一个可登录的 `95598` 账号，并且已经绑定户号。
- Home Assistant 已启用 MQTT 集成。
- 一个 MQTT Broker。使用官方 Mosquitto Add-on 时，`mqtt_host` 通常填 `core-mosquitto`。

## 安装

1. Home Assistant 进入 `设置` -> `加载项` -> `加载项商店`。
2. 右上角添加仓库：

```text
https://github.com/renxiaoyaoo/ha-95598
```

3. 找到 `95598 for Home Assistant` 并安装。
4. 在 `配置` 页面填写账号、密码和 MQTT 信息。
5. 启动 add-on，查看日志确认运行状态。

## 数据和配置

Add-on 的持久数据保存在 add-on 配置目录中，包括 SQLite 数据库、登录会话、二维码、页面快照和验证码样本。

电费估算依赖镜像内置的 `config/tou_price_config.json`。默认配置是湖南居民阶梯电价示例，不一定适用于你的地区；如果你的地区执行分时电价，需要自行调整谷、平、峰、尖价格。当前计算直接使用 `95598` 返回的分时用量和配置中的对应价格。

## 说明

本 add-on 是对主项目 Docker 镜像的包装，主项目文档见：

https://github.com/renxiaoyaoo/ha-95598
