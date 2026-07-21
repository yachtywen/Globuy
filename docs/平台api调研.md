# 国内主要电商平台商品 API 调研（globuy）

> 调研日期：2026-07-15  
> 调研对象：普通个人开发者为 `globuy` 的学习、演示和小规模离线召回测试获取商品数据。  
> 结论口径：优先采用平台官方开放平台、官方联盟和官方协议；登录后才能看到的权限，以实际
> 控制台审核结果为准。本文不是法律意见，也不把“存在 API”解释为可以无限量抓取、永久保存
> 或公开再分发平台数据。

## 1. 结论先行

用户提出的方案可以实现，但要把它准确描述为：

> 通过获批的联盟商品 API，小规模获取耳机为主的“可推广商品池”快照，归一化为
> `Product + Offer`，只把稳定商品语义写入 Faiss；价格、优惠、店铺等易变信息保存在报价层并
> 标记采集时间。

首批平台建议如下：

1. **淘宝/天猫：推荐，优先级 P0。** 普通个人可以先注册淘宝客、完成媒体备案并申请
   AppKey。淘宝联盟提供关键词物料搜索和商品详情接口，淘宝与天猫可以通过同一商品池接入，
   `user_type` 可区分淘宝/天猫商品。
2. **京东：推荐，优先级 P0。** 京东联盟明确存在个人账户，商品查询接口能提供 SKU、名称、
   主图、类目、价格、物流、自营标识等，是目前最适合做第二个 Provider 的国内平台。
3. **拼多多：有条件推荐，优先级 P1。** 多多进宝存在 API 推广和商品库能力，通常使用
   `pdd.ddk.goods.search`、`pdd.ddk.goods.detail` 或其 OAuth 版本；但公开文档页当前需要登录或
   前端渲染，个人主体是否能创建对应应用、具体权限包和限额必须在控制台二次确认。
4. **得物：首期不采用。** 当前能确认的得物开放平台主要服务得物商家、品牌直发和履约合作，
   没有发现普通个人可自助申请的全站消费者商品搜索 API。
5. **唯品会：候选 P2。** 个人可以成为联盟会员，但官网把“高收益 API 支持”主要放在机构
   开发者/工具商合作下，个人注册成功不代表自动获得商品搜索 API。
6. **抖音电商、快手电商：首期不采用。** 公开商品接口主要面向授权商户、服务商、小程序或
   电商合作场景，不是供普通个人搜索全站商品的通用联盟商品库。

因此，`globuy` 不需要等待所有平台开放。先完成“淘宝/天猫 + 京东 + 本地测试数据”，已经足以
验证跨平台统一 Schema、Faiss 召回、ItemPicker 和 PriceCompare；拼多多权限通过后再加入。

## 2. globuy 真正需要哪些数据

### 2.1 Faiss 召回所需的稳定商品字段

Faiss 不应直接保存一个平台页面的全部 JSON，更不应该把实时价格写进向量语义。建议 Item 塔
使用以下稳定字段生成 `item_embedding`：

| 字段 | 是否必需 | 耳机场景示例 |
|---|---:|---|
| `product_id` | 是 | globuy 内部标准商品 ID |
| `source_product_id` | 是 | 淘宝 item ID、京东 SKU ID 等 |
| `title` | 是 | Sony WH-1000XM5 头戴式降噪耳机 |
| `brand` | 建议 | Sony |
| `category` | 是 | 数码 > 耳机/耳麦 |
| `model` | 强烈建议 | WH-1000XM5 |
| `attributes` | 建议 | 头戴式、蓝牙、主动降噪、黑色 |
| `description_summary` | 可选 | 清洗后的稳定卖点摘要 |
| `source` | 是 | `taobao`、`tmall`、`jd`、`pdd` |

联盟 API 通常容易得到标题、类目、品牌、主图、店铺和价格，但“耳机材质、单元尺寸、编码格式、
蓝牙版本、电池时长”等细粒度规格不一定稳定返回。缺失规格应从有授权的详情字段、品牌官网或
人工整理的测试夹具补充，不能由模型凭空生成。

### 2.2 报价层所需的易变字段

同一款耳机可能在不同平台、不同店铺有多个报价。下列字段不进入 Faiss 语义向量，而进入
`Offer`：

| 字段 | 说明 |
|---|---|
| `offer_id` | 平台商品/SKU/活动的唯一报价 ID |
| `platform` | 淘宝、天猫、京东、拼多多等 |
| `shop_name` | 店铺名称 |
| `price` / `currency` | 当前接口返回的价格与币种 |
| `coupon` / `final_price` | 优惠券及预估到手价；必须说明计算口径 |
| `shipping_fee` | 有则保存，没有则标记未知 |
| `stock_status` | 只有官方接口明确返回时才写入 |
| `url` | 商品或联盟推广链接 |
| `captured_at` | 本次获取时间，判断价格是否过期 |
| `raw_source` | Provider 名称和 API 方法，不保存密钥 |

推荐数据关系：

```text
Faiss: faiss_id -> product_id -> 稳定商品语义

Product 1 ────── N Offer
Sony WH-1000XM5
  ├─ 天猫旗舰店报价
  ├─ 京东自营报价
  └─ 拼多多店铺报价
```

## 3. 平台可行性总表

| 平台 | 普通个人能否申请 | 公开商品查询路径 | 对 globuy 字段覆盖 | Faiss 小样本可行性 | 当前建议 |
|---|---|---|---|---|---|
| 淘宝/天猫 | 可以申请淘宝客；需实名、媒体备案和权限审核 | 淘宝联盟物料搜索 + 商品详情 | 标题、图片、类目、品牌、店铺、价格、销量、邮费等；深度规格和精确库存不保证 | 高，但仅限联盟商品池；持久化/向量化范围需遵守协议 | P0 |
| 京东 | 官方明确有个人联盟账户 | 京东联盟商品查询 + 推广商品详情 | SKU、名称、主图、类目、价格、物流、自营标识、30 天引单量等；材质/详细规格不保证 | 高，但仍是联盟商品池 | P0 |
| 拼多多 | 多多进宝面向推广者；个人应用资格需登录控制台实测 | DDK 商品搜索/详情 | 常见实现可得标题、图片、价格、券、店铺、销量、类目；官方字段以控制台为准 | 中高，前提是个人账号获批 API | P1 |
| 得物 | 未发现普通个人全站查询入口 | 开放平台偏商家和履约 | 授权商家数据可能很丰富，但不能查询任意全站商品 | 低 | 暂缓 |
| 唯品会 | 个人可加入联盟；API 权限未必向个人开放 | 联盟商品搜索/详情对机构开发者、工具商宣传开放 | 官网确认有搜索、详情、价格及促销能力 | 中，需单独商务/权限确认 | P2 |
| 抖音电商 | 可注册开发者，但商品接口依赖解决方案和商户授权 | 查询本应用或授权商户商品 | 能查授权商户商品，不是全站消费者商品池 | 低 | 暂缓 |
| 快手电商 | 可注册开放平台，电商能力按场景审核 | 小程序/电商商家/服务商商品接口 | 适合管理或挂载自身/授权商品，不是全站商品搜索 | 低 | 暂缓 |
| 苏宁易购 | 历史上存在推客/开放 API，当前公开入口和个人权限不够清晰 | 需实际联系开放平台确认 | 未取得当前可靠的个人商品搜索权限证明 | 低到中 | 不作为首批依赖 |
| 1688 | 主要面向采购、分销、商家与 ISV 授权场景 | 1688 代销/商家商品接口 | 可用于供应链商品，但不是普通消费者耳机报价的最佳来源 | 中低 | 后续全球供应链场景再评估 |

“可以申请”不等于“申请必过”；“接口免费”通常只表示接口本身不额外计费，不代表不需要
AppKey、签名、推广位、媒体备案、授权和流量限制。

## 4. 各平台具体操作

### 4.1 淘宝与天猫  网址：https://developer.alibaba.com/docs

#### 个人接入结论

可尝试，且最适合作为首个 Provider。淘宝联盟官方指南说明：淘宝客用户完成媒体备案后可以
创建 AppKey、查看权限包并申请 API；基础权限可自助申请，高级权限可能采用邀约制。参见
[淘宝联盟开发者新手指南](https://developer.alibaba.com/docs/doc.htm?articleId=118970&docType=1&treeId=713)。

淘宝和天猫不需要做成两个独立认证系统。淘宝联盟物料结果的 `user_type` 可区分店铺类型：
`0` 表示淘宝、`1` 表示天猫。

#### 推荐接口

- 关键词搜索：`taobao.tbk.dg.material.optional.upgrade`（推广者物料搜索升级版；最终以账号
  权限页显示的方法为准）。
- 单品详情：`taobao.tbk.item.details.upgrade.get`。官方页面标注免费且“不需要用户授权”，
  但调用仍需要 AppKey、AppSecret 签名。
- 推广链接：淘宝联盟对应的转链接口。

官方物料搜索文档列出的返回信息包括标题、主图、叶子类目、品牌、卖家/店铺、淘宝/天猫类型、
30 天销量、邮费、划线价、销售价和预估到手价等，足以生成耳机商品候选和报价快照。参见
[物料搜索升级版字段说明](https://developer.alibaba.com/docs/api.htm?apiId=64758&source=search)和
[商品详情升级版](https://developer.alibaba.com/docs/api.htm?apiId=64757)。

#### 操作步骤

1. 使用已实名认证的淘宝/支付宝账号登录[淘宝联盟](https://pub.alimama.com/)。
2. 注册淘宝客，按真实使用方式做网站、App 或“其他媒体”备案。
3. 在联盟开放平台基于已通过的媒体备案创建应用，取得 AppKey/AppSecret。
4. 创建推广位，记录 `site_id` 和 `adzone_id`。
5. 在能力/权限中心申请物料搜索、商品详情和转链权限。
6. 先在官方 API 调试工具中用关键词“耳机”“蓝牙耳机”“头戴式降噪耳机”验证返回字段。
7. 代码只在后端读取密钥，Provider 把原始返回归一化成 `Product + Offer`。

#### 限制

- 搜到的是淘宝联盟可推广商品，不是淘宝/天猫全量商品。
- 部分商品 ID、链接和消费者比价场景需要更高权限。
- 材质、完整 SKU、实时库存和用户评价正文不应假定必然存在。
- 公开文档证明了查询能力，但没有给本项目一项“可永久批量保存并训练/向量化”的通用授权；
  实际使用前需阅读账号签署的联盟协议和商品展示规则。

### 4.2 京东  网址：https://jos.jd.com/jdunion

#### 个人接入结论

可尝试，推荐作为第二个 Provider。京东联盟官方页面明确说明存在个人账户，并说明个人与企业
账户在提现发票处理上的差异。[京东联盟方案与接口说明](https://jos.jd.com/jdunion)

#### 推荐接口

- `jd.union.open.goods.query`：按 SKU、关键词、价格、优惠券等查询商品。
- `jd.union.open.goods.promotiongoodsinfo.query`：按 SKU 批量获取名称、主图、类目、价格、
  物流、自营标识和 30 天引单量等详情。
- `jd.union.open.promotion.common.get`：生成推广链接。

#### 操作步骤

1. 使用京东账号注册京东联盟，选择个人主体并完成实名认证。
2. 按真实推广方式登记网站、App、社交媒体或其他媒体；网站方式可能要求 ICP 备案。
3. 在京东联盟/宙斯开发者中心创建联盟应用，取得 AppKey/AppSecret。
4. 申请京东联盟商品查询、详情和转链权限。
5. 使用官方调试工具调用 `jd.union.open.goods.query`，分别测试关键词、价格区间和分页。
6. 用返回的 SKU 调用详情接口，确认耳机的品牌、类目、规格字段实际覆盖率。
7. 归一化后保存商品快照；实时展示前重新查询价格或标记 `captured_at`。

#### 限制

- 返回京东联盟推广商品，不等于京东全量商品。
- “价格”可能是京东价、券后价或预估到手价，必须保留字段来源和计算口径。
- 网站推广可能要求备案；账户或推广方式不合规可能被限权或封禁。
- 详细材质、蓝牙编码和耳机驱动单元等仍可能需要人工/品牌资料补全。

### 4.3 拼多多/多多进宝

#### 个人接入结论

有实现可能，而且已经确认 globuy 应从多多进宝 DDK 商品接口入手，而不是商家商品管理接口。
拼多多官方资料确认多多进宝提供 API 推广和商品库能力；
[多多进宝官方介绍资料](https://funimg.pddpic.com/ddjb/2020-12-04/4f8c0c46-e2c3-40e4-bfee-5ac05ba96607.pdf)
说明开发者可以通过 API 获得商品库接口。普通个人最终能否取得这些权限、实际限流以及数据
缓存范围，仍必须以自己登录后的应用控制台和协议为准。

#### 商家商品 API 与 DDK API 的区别

用户提供的两个文档链接帮助确认了接口边界：

- `pdd.delete.goods.commit` 是“删除商品接口”，只能删除授权商家自己的下架商品，属于商家
  商品管理，不是全站商品检索，globuy 不使用。
- [`pdd.ddk.goods.search`](https://open.pinduoduo.com/application/document/api?id=pdd.ddk.goods.search)
  是多多进宝商品搜索接口，属于导购/推广商品池，才是 globuy 构建耳机候选集的正确入口。

同理，`pdd.goods.add`、`pdd.goods.list.get`、`pdd.goods.update`、
`pdd.delete.goods.commit` 等新增、查询、编辑或删除“授权店铺自有商品”的接口，都不应加入
`PddJinbaoProvider`。它们既不能搜索拼多多全站，也会让应用申请不必要的商家写权限。

#### globuy 所需接口清单

| 接口 | 必要性 | 在 globuy 中的用途 | 是否需要写入 Faiss |
|---|---|---|---|
| [`pdd.ddk.goods.search`](https://open.pinduoduo.com/application/document/api?id=pdd.ddk.goods.search) | 必需 | 按“耳机”“蓝牙耳机”“降噪耳机”等关键词分页搜索多多进宝商品，取得候选商品和稳定的 `goods_sign` | 搜索结果归一化、去重后，仅稳定商品语义进入 Faiss |
| [`pdd.ddk.goods.detail`](https://open.pinduoduo.com/application/document/api?id=pdd.ddk.goods.detail) | 必需 | 按搜索结果的 `goods_sign` 补充商品详情、图片、类目、价格、优惠等字段 | 品牌、型号、类目和稳定规格可进入 Faiss；价格、券和销量只进入 `Offer` |
| [`pdd.ddk.goods.recommend.get`](https://open.pinduoduo.com/application/document/api?id=pdd.ddk.goods.recommend.get) | 可选 | 获取推荐/频道商品，用来补充耳机样本或相近品类干扰项；不能替代关键词搜索 | 同样先归一化和去重 |
| [`pdd.ddk.goods.promotion.url.generate`](https://open.pinduoduo.com/application/document/api?id=pdd.ddk.goods.promotion.url.generate) | 展示商品时需要 | 依据 `goods_sign` 和推广位生成合规跳转链接；离线训练/建索引阶段可暂不调用 | 不进入 Faiss，保存到 `Offer.url` |
| [`pdd.ddk.goods.pid.generate`](https://open.pinduoduo.com/application/document/api?id=pdd.ddk.goods.pid.generate) | 生成推广链接前需要 | 创建多多进宝推广位 PID | 不进入商品库 |
| [`pdd.ddk.goods.pid.query`](https://open.pinduoduo.com/application/document/api?id=pdd.ddk.goods.pid.query) | 可选 | 查询、校验当前账号已有 PID | 不进入商品库 |
| `pdd.ddk.oauth.*` 对应接口 | 首期不需要 | 只有当 globuy 要让其他多多客账号授权自己的应用时才使用 OAuth 版本 | 按对应非 OAuth 接口处理 |

首版最小闭环只需要两个核心查询接口：

```text
pdd.ddk.goods.search
  -> goods_sign 列表
  -> pdd.ddk.goods.detail
  -> Product + Offer
  -> Item 向量离线写入 Faiss
```

如果只是为本地 Faiss 构建测试集，不做返佣跳转，`promotion.url.generate` 和 PID 接口可以延后；
如果最终购物清单要给用户一个可打开的拼多多商品链接，再接入它们。

#### 操作步骤

1. 在[拼多多开放平台](https://open.pinduoduo.com/application/developer)完成开发者认证，确认账号
   能创建多多进宝/多多客应用，而不是商家自研或 ERP 应用。
2. 在多多进宝完成推广者注册和必要的媒体信息登记。
3. 创建应用，取得 `client_id`、`client_secret`；密钥只放 `.env`，不得提交或写入日志。
4. 在权限包中申请 `pdd.ddk.goods.search` 和 `pdd.ddk.goods.detail`；若要输出推广链接，再申请
   PID 与推广链接生成能力。
5. 按官方公共参数规则提交 `type`、`client_id`、`timestamp`、`data_type`、`sign` 等参数；
   是否需要 `access_token` 由当前接口页面的“必须用户授权”标记和账号权限决定。
6. 首次只搜索 20 条“蓝牙耳机”，保存脱敏字段覆盖报告，确认 `goods_sign`、标题、图片、类目、
   店铺/商城、价格和优惠字段的实际返回情况。
7. 再用 `goods_sign` 调详情接口，检查品牌、型号和关键规格覆盖率；不得由 LLM 编造缺失规格。
8. 账号协议明确允许所需缓存/内部检索用途后，才扩大为本文建议的小规模耳机快照。

#### 创建应用审核：官网地址与回调地址

拼多多创建多多客联盟应用的表单要求填写“回调地址”和“官网地址”。二者建议使用同一个自有
域名下的不同路径，但用途不同：

| 表单字段 | 推荐示例 | 用途 |
|---|---|---|
| 官网地址 | `https://www.example.com` | 审核人员查看项目名称、用途、隐私说明和联系方式的公开页面 |
| 回调地址 | `https://api.example.com/integrations/pinduoduo/oauth/callback` | OAuth 授权完成后接收平台重定向；必须是后端可处理的稳定 HTTPS 地址 |
| 相关链接 | `https://www.example.com/about`、`https://www.example.com/privacy` | 可选，辅助说明产品和数据使用边界 |

其中 `example.com` 必须替换为申请人真实持有并可控制解析的域名。审核时不要填写
`localhost`、局域网地址、临时内网穿透域名或尚未部署的空地址。回调 URL 应至少能够正常建立
HTTPS 连接并返回可识别响应，正式实现 OAuth 时还要校验 `state`、处理授权码，并且不得把
Token 输出到页面或日志。

可以把项目部署在阿里云，推荐分两阶段：

1. **认证最小站点**：先部署公开项目介绍页、隐私说明、联系方式和一个 FastAPI 回调路由；
   不需要为了提交审核提前运行完整 Faiss/OpenSearch。
2. **完整学习环境**：审核和接口验证完成后，再部署 React、FastAPI、WebSocket、Faiss 和
   OpenSearch；测试环境仍保持外部工具未配置时返回 `not_configured`。

若服务器选择阿里云中国内地地域，域名对外提供网站服务前需要完成 ICP 备案；若选择中国香港
或海外地域，通常不走中国内地服务器的 ICP 备案流程，但这不代表拼多多一定免除其自身的应用
审核、主体或网站证明要求。当前创建应用截图只明确要求填写 HTTP/HTTPS URL，没有证明该应用
类型强制要求 ICP，因此最终以提交后的审核反馈为准。

不能用“只提供 `http://公网IP`”规避中国内地服务器备案。阿里云官方说明，中国内地服务器上
即使网站没有绑定域名、仅通过 IP 对外提供服务也需要 ICP 备案；而阿里云备案系统只支持域名
备案，不支持单独对 IP 备案。因此公网 IP 只适合部署调试，不应作为拼多多应用审核的正式官网
或长期回调地址。

推荐部署顺序：购买并实名认证域名 -> 选择服务器地域 -> 中国内地服务器完成 ICP 备案 ->
DNS 解析 -> 部署站点与回调路由 -> 配置受信任的 SSL 证书 -> 用公网浏览器验证所有 URL ->
再提交拼多多应用审核。阿里云官方也把域名注册、服务器、ICP备案、部署、解析和 HTTPS 列为
网站上线的完整链路：
[阿里云网站搭建全流程](https://help.aliyun.com/zh/dws/getting-started/the-whole-process-of-website-building)、
[SSL 证书部署方案](https://help.aliyun.com/zh/ssl-certificate/ssl-certificate-deployment-scheme-selection)、
[IP 访问网站的备案要求](https://help.aliyun.com/zh/icp-filing/basic-icp-service/product-overview/icp-filing-requirements-for-a-regular-website)。

#### 限制

- `pdd.ddk.goods.search` 文档存在，不等于当前个人应用自动拥有调用权限；必须查看相关权限包。
- 正式字段、调用额度、是否需要 OAuth 必须以登录后的官方控制台为准。
- 新实现以 `goods_sign` 作为跨搜索、详情和生链的来源商品标识，不依赖旧教程的纯数字
  `goods_id`。
- DDK 搜索的是多多进宝可推广商品池，不是拼多多全量商品。
- 搜索/详情返回的价格、优惠、销量等都是带时间性的 `Offer` 快照，不能作为稳定 Item 语义。
- 个人账号能否批量保存并向量化，仍需根据实际签署协议确认；公开文档本身不是数据再利用授权。

### 4.4 得物

#### 个人接入结论

暂不满足本项目需求。当前[得物开放平台](https://open.dewu.com/prerenderSpaIndex.html)公开内容
集中在商家接入、品牌直发、订单、物流和履约，服务对象是得物商家及合作伙伴。调研未找到：

- 普通个人开发者自助创建“全站商品搜索”应用的入口；
- 面向导购者的官方联盟商品池；
- 普通个人可直接申请的商品详情/实时报价 API。

网上出现的 `api.dewu.com/product/detail` 示例、抓包接口或收费“得物商品 API”不能视为得物
官方授权接口，不应作为 globuy 的数据源。若未来要接入，只接受得物开放平台明确授予的商家/
合作伙伴权限或书面合作方案。

### 4.5 唯品会

唯品会联盟协议允许个人和机构申请联盟会员，但平台保留审核决定权。官网同时列出了商品搜索、
商品详情、图片搜索、推广链接等 API 能力，不过这些能力展示在“机构开发者/工具商合作”部分。
参见[唯品会联盟首页](https://union.vip.com/)和
[唯品会联盟协议](https://union.vip.com/bulletin/xieyi)。

因此正确结论是：

- 个人可以注册推广；
- 个人账户不一定自动获得搜索 API；
- 若要作为 Provider，应先通过官网合作入口或 `lianmeng@vipshop.com` 询问“个人学习项目能否
  申请商品搜索/详情 API、能否缓存少量商品用于检索演示”。

### 4.6 抖音电商与快手电商

抖音公开的商品查询接口要求申请特定解决方案权限并获得商户授权，查询的是本应用或授权商户
商品，不是全站商品池。官方文档明确写有商户授权要求：
[抖音商品线上数据列表](https://partner.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/general-capabilities/product-query/online.query)。

快手开放平台提供电商、小程序和商品挂载能力，但公开资料同样指向开发者自身或合作商户的商品
场景，而不是普通个人对快手全站商品做关键词搜索。参见
[快手开放平台](https://open.kuaishou.com/platform/openApi?grop=GROUP_OPEN_PLATFORM)。

两者后续若出现明确的个人分销商品库/API，再加入 Provider；首期不要通过抓包 App 私有接口来
替代官方授权。

## 5. 耳机 Faiss 测试集推荐方案

### 5.1 数据规模

该项目不是大型生产系统，不需要为了“像生产”而拉取数百万条商品。第一版建议：

| 数据 | 建议数量 | 占比 |
|---|---:|---:|
| 耳机/耳麦平台报价 `Offer` | 800～1200 | 75%～85% |
| 干扰报价 | 150～300 | 15%～25% |
| 去重后的标准商品 `Product` | 约 300～700 | — |

干扰类目可以选择与耳机相近、容易混淆的商品：音箱、麦克风、耳机线、耳机保护壳、USB 声卡，
再加入少量完全不相关的键盘、鼠标、充电器。这样比加入大量随机商品更能测试召回边界。

建议来源配比：

```text
淘宝/天猫联盟：40%
京东联盟：40%
拼多多：      0%～20%（权限获批后加入）
本地明确标记的测试夹具：用于补足负样本，不伪装成实时平台商品
```

### 5.2 采集与建库流程

```text
联盟 API
  -> Provider 原始响应（仅短期调试）
  -> 字段白名单与标准化
  -> Product/Offer 去重
  -> 耳机规格补全与质量检查
  -> Item 塔生成稳定商品向量
  -> Faiss IndexHNSWFlat + IP
  -> faiss_id/product_id 映射表
```

推荐只把下面的文本送入 Item 塔：

```text
品牌 + 型号 + 标准类目 + 佩戴方式 + 有线/无线 + 降噪类型 + 主要稳定规格 + 清洗标题
```

不要将以下内容嵌入 Item 向量：实时价格、优惠券金额、库存、销量、佣金率、广告文案。否则每次
价格变化都需要重算向量，而且相似度会被商业字段污染。

### 5.3 API 数据并不足以训练完整三塔模型

商品 API 主要提供 Item 内容和报价，不提供可自由用于训练的真实用户点击、搜索、加购和购买
日志。因此它足以：

- 建立 Item 语料和 Faiss 索引；
- 测试 Query 到 Item 的召回链路；
- 测试跨平台报价聚合。

但它不足以直接训练完整的 User/Query/Item 三塔。训练阶段仍需合法的公开数据集、人工构造并
审核的查询—商品相关性样本，或项目自己产生的测试交互数据；不得抓取用户隐私或平台内部行为
日志。

## 6. 推荐实施顺序

### 阶段 A：不等平台审核，先完成工程闭环

1. 定义 `Product`、`Offer`、`SourceSnapshot` Pydantic Schema。
2. 创建 `MockCommerceProvider` 和明确标注来源的本地耳机夹具。
3. 完成去重、Item 文本构造、Faiss 建库和 `ItemSearch` 信息补全。
4. 用 30～50 条自然语言查询建立召回评测集，例如“预算 500、头戴式、通勤降噪”。

### 阶段 B：申请两个首批真实 Provider

1. 申请淘宝联盟媒体备案、AppKey、物料搜索和详情权限。
2. 申请京东联盟个人账号、媒体、AppKey 和商品查询权限。
3. 每个平台先拉取 20 条，输出字段覆盖率和权限错误报告。
4. 只有在账号协议允许后，才批量构建 800～1200 条小规模快照。

### 阶段 C：条件接入拼多多

1. 登录拼多多开放平台确认个人主体和 DDK 权限。
2. 确认 `goods_sign`、OAuth、调用额度和数据使用规则。
3. 获批后实现 `PddProvider`；未获批则保持 `not_configured`，不能转而使用 App 抓包接口。

## 7. Provider 代码边界建议

```text
app/providers/
  base.py                 # CommerceProvider 协议、错误模型
  mock.py                 # 本地测试夹具
  taobao_union.py         # 淘宝/天猫联盟
  jd_union.py             # 京东联盟
  pdd_jinbao.py           # 多多进宝，获批后启用
  dewu.py                 # 暂只返回 not_configured/unsupported

app/catalog/
  schemas.py              # Product / Offer / SourceSnapshot
  normalizers/
  deduplicate.py
  repository.py           # 商品元数据，不等于 Faiss

app/recall/
  build_index.py          # Item 向量与 Faiss HNSW/IP
  ann_client.py
```

每个 Provider 都应返回：

- `source`、`source_product_id`、`fetched_at`；
- 明确的 `not_configured`、`permission_denied`、`rate_limited`、`partial_data`；
- 原字段到统一 Schema 的映射报告；
- 不打印 AppSecret、Token、Cookie。

## 8. 数据与合规检查清单

在任何真实平台批量入库前逐项确认：

- [ ] 账号主体和媒体备案是真实信息。
- [ ] 使用官方 API，而不是移动 App 抓包、Cookie 模拟登录或绕过验证码。
- [ ] 账号已获得对应商品搜索/详情权限。
- [ ] 协议允许本项目所需的缓存时间、字段展示和内部检索用途。
- [ ] 若要持久保存并生成向量，已确认这不违反数据许可；无法确认时只做短时缓存和实时查询。
- [ ] 不下载并重新分发全量图片；优先保存官方图片 URL，并遵守展示规则。
- [ ] 不保存用户隐私、订单或行为日志。
- [ ] 价格、优惠、库存全部带 `captured_at`，过期时不宣称“实时”。
- [ ] 不公开发布平台原始全量 JSON 数据集或 AppKey。
- [ ] 对每个平台设置独立限流、重试、熔断和删除/刷新机制。

“仅用于学习、数据量很小、没有正式上线”会降低工程规模，但不会自动豁免平台协议。因此最稳妥
的做法是：先用本地夹具完成代码，再使用账号获批 API 做小批量数据验证。

## 9. 实施前的账号实测验收表

每个平台获批后必须完成下面的测试，结果再写回本文件：

| 验收项 | 通过标准 |
|---|---|
| 关键词搜索 | “蓝牙耳机”返回至少 20 条非空结果 |
| 商品标识 | 能得到稳定商品/SKU/签名 ID |
| 标题与类目 | 非空率 >= 95% |
| 品牌 | 非空率达到可接受水平，缺失可标记 unknown |
| 店铺与平台 | 能区分来源；淘宝结果能区分淘宝/天猫 |
| 报价 | 明确原价、销售价、券后价的字段口径 |
| 图片 | 有可合法展示的 URL，不批量下载图片二进制 |
| 规格 | 统计耳机关键规格覆盖率，不用 LLM 编造缺失值 |
| 权限/限流 | 记录实际 QPS、日额度和错误码 |
| 数据许可 | 记录缓存、展示、向量化和删除要求 |

## 10. 官方来源与证据强度

### 高可信官方来源

- [淘宝联盟开发者新手指南](https://developer.alibaba.com/docs/doc.htm?articleId=118970&docType=1&treeId=713)：注册淘宝客、媒体备案、AppKey 和权限申请流程。
- [淘宝联盟物料搜索升级版](https://developer.alibaba.com/docs/api.htm?apiId=64758&source=search)：搜索条件和标题、品牌、店铺、价格等返回字段。
- [淘宝客商品详情升级版](https://developer.alibaba.com/docs/api.htm?apiId=64757)：单商品详情接口及签名调用示例。
- [京东联盟方案与接口](https://jos.jd.com/jdunion)：个人账户、商品查询、详情和转链接口说明。
- [得物开放平台](https://open.dewu.com/prerenderSpaIndex.html)：当前公开定位为商家/品牌直发与履约合作。
- [唯品会联盟](https://union.vip.com/)：个人推手、机构开发者及商品 API 能力分层。
- [唯品会联盟协议](https://union.vip.com/bulletin/xieyi)：个人/机构主体、审核和推广规则。
- [抖音商品查询接口](https://partner.open-douyin.com/docs/resource/zh-CN/local-life/develop/OpenAPI/general-capabilities/product-query/online.query)：解决方案权限和商户授权要求。
- [快手开放平台](https://open.kuaishou.com/platform/openApi?grop=GROUP_OPEN_PLATFORM)：开放能力和电商能力入口。

### 需要登录后二次核验

- [拼多多开放平台](https://open.pinduoduo.com/)：公开站点/API 明细依赖登录和前端渲染。
- [多多进宝官方介绍资料](https://funimg.pddpic.com/ddjb/2020-12-04/4f8c0c46-e2c3-40e4-bfee-5ac05ba96607.pdf)：确认存在 API 推广和商品库能力，但不能替代当前账号的权限页。

本文刻意不把收费聚合 API、逆向抓包服务、博客中声称的“免授权商品 API”作为授权依据。
