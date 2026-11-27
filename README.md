# 期市链条 · 商品温度看板

期货产业链热度看板：62 个品种的日线 / 60 分钟 / 30 分钟技术状态、家族强弱、钢铁产业链对照。
数据源为新浪财经期货接口，页面是纯静态 HTML + ECharts，由 GitHub Actions 定时重建并部署到 GitHub Pages。

- 站点：https://Tttst00001.github.io/futures-dashboard/
- 构建状态明细：https://Tttst00001.github.io/futures-dashboard/build-info.json

## 目录

```
scripts/                取数与生成脚本（market_dashboard.py 为入口）
web/dashboard_template.html  页面模板（数据以 __DATA_JSON__ 占位注入）
web/vendor/echarts.min.js    图表库
seed/data-market.tar.gz      行情缓存种子（缓存未命中时用于冷启动）
```

## 更新节奏

时间均为北京时间，周一至周五：

| 周期 | 触发时间 | 更新内容 |
| --- | --- | --- |
| 30 / 60 分钟 | 10:00 / 11:15 / 14:15 / 15:00 / 22:00 / 23:00 | 分钟线（日线当日已入库则跳过） |
| 日线 | 17:00 | 收盘后日线 |

是否重新抓取由缓存 TTL 决定：日线一天一次（16:00 之后才允许刷新当日 bar），分钟线 30 分钟 TTL；主力合约识别 6 小时一次。

## 本地运行

```bash
pip install numpy "MyTT==2.9.3"
python scripts/market_dashboard.py --workers 8          # 增量更新
python scripts/market_dashboard.py --workers 8 --force  # 忽略缓存全量重抓
python scripts/market_dashboard.py --serve --port 8080  # 生成后本地起服务
```

产物为 `web/index.html` 与 `data/market/snapshot.json`；工作流把 `web/index.html` 与 `web/vendor/` 打包成 Pages 产物，并额外输出 `build-info.json` 便于线上核对抓取时间与异常条数。

## 说明

- 新浪分钟线的 `v`（成交量）字段不代表真实区间成交量，量能判断一律用日线。
- 分钟线夜盘（22:00 / 23:00）记在当晚日历日，与日线交易日口径不同。
- 页面内嵌快照，浏览器端不再请求行情；刷新依赖上面的定时重建。
- 弹层三张 K 线（30 分钟 / 1 小时 / 日线）按 KDJ 的 K 与 D 关系着色背景：K>D 浅红，K≤D 浅绿。
- 弹层的 1 小时与日线图在最近一次信号转换（K/D 翻转）那根 bar 上标注：该 bar 收盘价画横线（多头深红、空头深绿），并在该 bar 上方标一个橙色圆点。
