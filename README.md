# 每日金融市场全景看板

GitHub Actions 每天北京时间 9:00 自动抓取全球金融数据，生成 HTML 看板，部署到 Cloudflare Pages。
**完全免费，不需要电脑开机，手机/电脑均可访问。**

## 看板覆盖五大维度

| 维度 | 数据 |
|------|------|
| 经济基本面 | PMI / CPI / PPI / M1 / M2 / 剪刀差 |
| 情绪面 | 上证 / 深证 / 创业板 / 科创50 / 涨跌家数 / 涨停跌停 / VIX / 恐惧贪婪指数 |
| 资金面 | 两融余额 / 北向资金 |
| 外部市场 | 美元指数 / 美元人民币 / 黄金 / 白银 / 铜 / 原油 / 比特币 / 以太坊 / 美股三大指数 / 恒生指数 |
| 美债 & 风险 | 美债 2Y/10Y/30Y 收益率 / 10Y-2Y 利差 |

## 数据源

- **yfinance** — A股指数、美股、外汇、大宗商品、美债、VIX（海外 IP 稳定）
- **akshare** — A股板块、涨跌停家数、两融、北向资金、宏观经济（容错处理）
- **CoinGecko API** — 比特币、以太坊
- **Alternative.me API** — 恐惧贪婪指数

## 部署步骤（约 10 分钟）

### 第 1 步：上传文件到 GitHub 仓库

将以下文件上传到你刚创建的 `finance-dashboard` 仓库：

```
finance-dashboard/
├── .github/
│   └── workflows/
│       └── daily.yml        # GitHub Actions 定时任务
├── dashboard.py             # 主脚本
├── requirements.txt         # Python 依赖
└── README.md                # 本文件
```

**上传方法**（在 GitHub 仓库页面操作）：
1. 点击 **Add file → Upload files**
2. 把 `dashboard.py`、`requirements.txt`、`README.md` 拖进去
3. 对于 `.github/workflows/daily.yml`，需要先点 **Create new file**，手动输入路径 `.github/workflows/daily.yml`，然后粘贴内容
4. 点击 **Commit changes**

### 第 2 步：注册 Cloudflare 并获取凭证

1. 登录 **https://dash.cloudflare.com**
2. 获取 **Account ID**：
   - 右侧边栏，或点击任意进入页面后看 URL 中的 account ID
   - 也可以在 Workers & Pages 页面右下角找到

3. 创建 **API Token**：
   - 进入 **My Profile → API Tokens → Create Token**
   - 选择模板 **"Edit Cloudflare Workers"**
   - 在 Account Resources 中选择你的账户
   - 点击 **Continue to summary → Create Token**
   - **复制 Token**（只显示一次！）

### 第 3 步：在 GitHub 仓库配置 Secrets

1. 进入你的 GitHub 仓库页面
2. 点击 **Settings → Secrets and variables → Actions**
3. 点击 **New repository secret**，添加两个：

| Name | Value |
|------|-------|
| `CLOUDFLARE_API_TOKEN` | 第 2 步复制的 API Token |
| `CLOUDFLARE_ACCOUNT_ID` | 第 2 步获取的 Account ID |

### 第 4 步：手动触发首次部署

1. 进入仓库的 **Actions** 标签页
2. 左侧选择 **Daily Finance Dashboard**
3. 点击右侧 **Run workflow → Run workflow**
4. 等待 2-3 分钟，绿色 ✅ 表示成功

### 第 5 步：获取访问链接

1. 登录 Cloudflare → **Workers & Pages**
2. 你会看到一个名为 `finance-dashboard` 的项目
3. 点击进去，顶部会显示访问链接：
   ```
   https://finance-dashboard-xxxx.pages.dev
   ```
4. 这个链接就是你的看板地址，**手机和电脑都能打开**

## 验证 & 日常使用

| 操作 | 说明 |
|------|------|
| 查看部署日志 | GitHub 仓库 → Actions → 点击最近的运行记录 |
| 手动触发 | Actions → Daily Finance Dashboard → Run workflow |
| 查看部署历史 | Cloudflare → Workers & Pages → finance-dashboard → Deployments |
| 每日自动更新 | 每天 UTC 01:00（北京时间 09:00）自动运行 |

## 常见问题

### Q: Actions 运行失败怎么办？
A: 进入 Actions 页面，点击失败的运行记录查看日志。最常见的原因是 Cloudflare Secrets 配置错误，检查 Token 和 Account ID 是否正确。

### Q: Cloudflare 部署失败 "project not found"？
A: 首次运行时 cloudflare/pages-action 会自动创建项目。如果失败，可以手动在 Cloudflare Workers & Pages 页面创建一个名为 `finance-dashboard` 的项目。

### Q: 数据有缺失怎么办？
A: yfinance 的数据在海外 IP 上最稳定。akshare 部分接口在 GitHub Actions 的美国 IP 上可能不稳定，脚本已做容错处理，获取失败的字段会显示"暂无数据"。

### Q: 想修改看板样式？
A: 编辑 `dashboard.py` 中的 `CSS` 变量和 `generate_html()` 函数，提交到仓库即可。下次运行自动生效。

### Q: 想修改运行时间？
A: 编辑 `.github/workflows/daily.yml` 中的 cron 表达式。例如 `0 3 * * *` = 北京时间 11:00。

## 技术架构

```
GitHub Actions (UTC 01:00 / 北京 09:00)
    ↓
  安装 Python 依赖
    ↓
  运行 dashboard.py
    ├── yfinance 获取全球行情
    ├── akshare 获取 A 股板块/两融/北向/宏观
    ├── CoinGecko 获取加密货币
    └── 生成 index.html (含 ECharts 图表)
    ↓
  cloudflare/pages-action 部署
    ↓
Cloudflare Pages CDN
    ↓
https://finance-dashboard-xxxx.pages.dev
  (手机/电脑均可访问)
```

## 颜色规则

中国股市颜色规则：涨 → 红色 (#c0392b)，跌 → 绿色 (#27ae60)

## License

MIT
