# 飞书视频数据抓取 Webhook 部署说明

## 需要部署的文件

- `feishu_media_metrics_sync.py`
- `feishu_media_metrics_webhook.py`
- `requirements.txt`
- `render.yaml`

## Render 部署

1. 新建一个 GitHub 仓库，把以上文件上传进去。
2. 打开 Render，选择 `New` -> `Blueprint`。
3. 选择这个 GitHub 仓库。
4. Render 会读取 `render.yaml` 自动创建 Web Service。
5. 在 Render 的环境变量里填写：

```text
FEISHU_APP_ID=你的飞书应用 App ID
FEISHU_APP_SECRET=你的飞书应用 App Secret
WEBHOOK_TOKEN=一段随机密钥
```

`FEISHU_BASE_TOKEN` 和 `FEISHU_TABLE_ID` 已经写在 `render.yaml` 里：

```text
FEISHU_BASE_TOKEN=Sj0XbUehqasIgGsTB1JcVsbMn4e
FEISHU_TABLE_ID=tblCYLbbNQLVQjcq
```

## 飞书自动化流程配置

触发器：

```text
寄样表 中 视频发布链接 填写后触发
```

建议触发条件：

```text
视频发布链接 不为空
且
点赞 为空
或 评论数量 为空
或 收藏 为空
或 播放量 为空
```

动作：

```text
发送 HTTP 请求
```

请求方式：

```text
POST
```

URL：

```text
https://你的-render域名.onrender.com/metrics
```

Header：

```text
Authorization: Bearer 你的 WEBHOOK_TOKEN
Content-Type: application/json
```

Body：

```json
{
  "record_id": "{{第1步.记录ID}}",
  "video_url": "{{第1步.视频发布链接}}"
}
```

## 行为说明

- 只写入空字段，不覆盖已有数据。
- 抖音公开页一般能抓到点赞、评论、收藏；播放量经常不公开，抓不到时不写。
- B站走公开 API，通常较稳定。
- 小红书公开页可能受登录和反爬影响，抓不到时会返回错误。
