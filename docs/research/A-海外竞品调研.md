# 海外 AI 银行助手与 Agentic Banking 竞品调研

> 截止日期：2026 年 10 月 28 日。本文以银行、产品方、支付网络的公开材料为主；“上线”区分正式开放、分批开放、受控试点和未来计划。能力矩阵只评价**该助手对普通客户已经公开证实的能力**，不把银行 App 原有菜单功能、员工内部工具或演示交易算作助手能力。国家、账户类型和版本可能不同。未找到可靠披露的项目标为“未证实”。

## 一、核心结论

1. **Revolut AIR 确有其名，且已跨过纯问答。**2026 年 4 月 9 日开始在英国分批推出，9 月在欧洲多地扩展。它能分析支出、查投资、冻结卡、暂停周期扣款、限制商户支付、申请一次性虚拟卡和购买 eSIM；敏感动作需生物识别批准。但公开材料没有证明 AIR 可以直接按自然语言发起银行转账、申购理财或完成第三方订阅解约。[1][2][3][4]
2. **“能交易”的成熟实例早于大模型。**U.S. Bank Smart Assistant 在 2020 年已能语音发起 Zelle 转账，要求用户确认；Chase Digital Assistant、CommBank Ceba 等也能完成部分付款或卡片操作。这说明可控业务 API、身份认证和确认闭环比“是否采用 LLM”更决定实际能力。[12][14][26]
3. **2026 年新一代产品从账单问答转向有限代理。**Starling Assistant 可创建储蓄目标、安排账单、锁卡、转入储蓄 Space，且每个账户操作前询问批准；Cleo Autopilot 可在确认后转入储蓄、设置商户限额。NatWest、Lloyds、Monzo 的面客能力仍主要围绕消费洞察、储蓄规划或试点。[17][20][21][23]
4. **理财“能查”与“能买”之间有明显断层。**Erica、AIR、Chase 可展示投资信息，但未证实其面向普通客户的对话助手可自主完成基金申购/赎回。Revolut X 允许第三方 AI 准备加密资产订单，且规定用户审阅批准；它是另一个产品，不应混作 AIR 的理财能力。[5][10][15]
5. **代理支付形成“授权证据链”共识。**Google AP2 以 Intent/Cart Mandate 固定用户意图、价格和条件；ACP 将支付令牌限定商户、金额与有效期；Visa、Mastercard、PayPal 侧重代理身份、令牌化、限额、认证和交易可追溯。上述是支付基础设施，不等于任何一家银行的 AI 助手已开放无限制自动付款。[34][35][36][37][38]
6. **差异化空间在跨场景执行与可解释安全。**多数银行已有账单查询、卡片自助和提醒；较少公开证明“事件触发→预算预留→跨商户采购→逐笔授权→可撤销/可追责”的完整链路。比赛演示宜把此链路做实，并清楚展示绿、黄、红权限，而非只堆砌聊天入口。[1][17][34]

## 二、Revolut AIR 深度分析

### 2.1 上线、市场与渠道

Revolut 官方将 AIR 展开为 **AI by Revolut**。2026 年 4 月 9 日宣布在英国面向约 1300 万客户逐步推出；9 月 1 日的法国站博客与法国、奥地利等国家页面显示欧洲市场已扩展，但各国可用功能、覆盖比例及具体到户日期未统一公开。入口是 Revolut App 主屏中部下拉，或“个人资料→Chats→AIR”；官方把它描述为应用内对话式执行引擎。[1][2][3][4]

公开产品页和帮助页能确认文本聊天，投资数据有价格/收益展示，某德国实测称一般回复主要为文本，入口较隐蔽。Revolut 的法律说明区分 **AI Support**（客服聊天、部分国家语音）与 **AI Revolut Assistant/AIR**（财务与旅行助手）；不能把前者语音能力算给 AIR。AIR 的语音输入、独立 IM 渠道、富卡片交互是否上线，未证实。[6][7]

### 2.2 实际业务边界

官方举例包括：问咖啡等分类支出、预算与可储蓄额；查询股票价格、个人投资收益和持仓信息；查看订阅、暂停定期付款、阻止特定商户后续扣款；冻结卡、设置卡支出限额、申请一次性虚拟卡；查汇率、购入旅行 eSIM。[1][2][3] 因此它**不只是查询机器人**，但公开可核验的执行以卡、商户支付控制和 eSIM 为主。“暂停周期付款/阻止商户扣款”不等于代用户在商户侧取消合同，也没有证据显示 AIR 已支持按姓名或手机号转账、定时转账、AA 收款、理财买卖和跨商户生活采购。投资信息被标注不构成投资建议。[2][3][6]

另有 **Revolut X** 于 2026 年 6 月开放外部 AI 连接，支持 Claude、Gemini、OpenClaw、Cursor、Codex 等查询、提醒、准备加密资产订单；所有订单须由用户审阅批准。它可作为外部智能体接入参考，但属于加密交易平台连接，不能推断 AIR 自身有这些交易权限。[5]

### 2.3 安全、模型及反馈

AIR 官方承诺只读取用户在 App 中本可见的数据，第三方 AI 伙伴零留存、不用于训练外部模型；**敏感动作须生物识别批准**。法律说明称会按请求向技术伙伴披露必要数据，交互通常会记录用于质量评估，回答可能有误，复杂情况可转人工客服。其具体金额限额、分级强认证阈值、模型名称、供应商名单、提示注入防护实现、回退机制与审计日志对客户的可见性，公开资料未证实；“与 OpenAI 合作推广 ChatGPT”也不能推断 AIR 必然使用 OpenAI 模型。[3][6][8]

公开用户反馈还很有限。德国支付行业媒体的数日实测指出入口不显眼、回复多为文本；Reddit 个别英国用户反馈下拉未出现 AIR，与官方“逐步推出”一致，但样本不能代表整体满意度。Revolut 公布的客服 AI 满意度或处理率也不是 AIR 独立指标；截至截止日未见可靠 AIR 独立活跃用户数、成功执行率和交易错误率。[2][7][9]

## 三、各产品逐个分析

### 3.1 美国银行与金融科技

**Bank of America Erica。**2018 年 6 月在美国移动 App 上线，支持文字和语音；官方称截至 2025 年 8 月累计超 30 亿次交互、约 5000 万用户，主动洞察超 17 亿条。可查交易、现金流、订阅和投资主题，并帮助转账/付账、约见顾问，复杂问题转人工；2023 年年报明确提及支付能力。核心是银行自研 NLP、预测分析与持续维护的应答库，并非已证实的自主 LLM 交易代理。单笔认证、限额和模型厂商未公开。[10][11]

**Capital One Eno。**2017 年推出美国短信自然语言助手，后来也通过 App 通知与浏览器扩展提供账户/消费提醒、异常或重复扣款提示、信用卡账单支付和商户专用虚拟卡号。其早期技术路线明确为 NLP/规则化任务交互；当前是否采用某个生成模型、能否经聊天发起对外转账或取消订阅，未证实。亮点是短信触达与“事前发现异常→用户处理”的轻量闭环。[12][13]

**Wells Fargo Fargo。**2022 年宣布与 Google Cloud 合作，2023 年在美国移动 App 对客户推出，后增加西语；提供余额、消费模式、账户服务和 Fargo Insights。公开资料说明 Google Cloud AI 合作，但没有足够证据认定其经对话直接执行转账、理财申购或复杂卡操作；“近 6 亿次自主客户交互”是该行 2025 年报告口径，不等同 6 亿笔交易。确认和强认证细则未披露。[14]

**JPMorgan Chase。**个人客户可在 Chase Mobile 使用 **Chase Digital Assistant** 查账户、锁/解卡、换卡、付款/转账及看投资观察列表；帮助页同时说明部分请求只是导航到 App 页面，故逐项是否由助手直接完成需谨慎。官方未标明该助手首次上线日期、底层模型和统一金额限额。另有 J.P. Morgan Payments 于 2025 年 10 月上线面向商户的 Commerce Center 报表生成式 AI 助手，以 RAG 和幻觉评测回答报表问题；这不是消费者转账代理。[15][16]

**Citi。**2020 年美国商业卡语音 IVA 用 NLP 识别来电意图；2025 年 CitiDirect 商业银行平台提供 AI 辅助资料提取、查询路由、欺诈识别和下一步提示，2026 年投资者资料列出 CitiDirect AI Assistant。其多数公开 GenAI 产品如 CitiService Agent Assist、Citi Assist、Arc 首先服务员工或企业客户；没有证据证明美国个人银行客户已获一个覆盖六场景且可自行执行交易的通用 AI Agent。外部模型、面客交易授权细节未证实。[29][30][31]

**U.S. Bank Smart Assistant。**2020 年 7 月在美国 App 推出，支持语音和文字；2022 年有西语版，现亦见于网银。可解析“我欠某人午餐钱”等模糊说法、追问改金额/日期，处理 Zelle 汇款、账户间转账、付账、查消费与锁/解卡。官方示例要求用户说“yes”确认。数字服务协议强调语音不是身份认证，用户须核对屏幕上的付款指令，银行留存命令与回应记录；模型厂商未披露。它是比赛场景 1 最直接的成熟对标。[17][18]

### 3.2 英国及欧洲

**NatWest Cora+。**原 Cora 已服务客户多年；2024 年 6 月与 IBM 推出生成式升级 Cora+，2025 年 3 月宣布与 OpenAI 合作。2026 年初，OpenAI 模型支撑的消费洞察代理在 Cora 内向约 2.5 万客户试点；后续语音消费洞察仍属拟测试方向，欺诈分诊已有上线案例。App 聊天、消费问答、欺诈求助较明确；理财买卖、转账或代扣取消由 Cora+ 直接执行均未证实。银行强调数据加密、匿名化与 AI 伦理治理。[19][20]

**Lloyds Banking Group。**2025 年 7 月公布的 **Athena** 是员工知识检索，不能写成面客助手。集团于 2025 年 11 月宣布面向客户的 AI Financial Assistant，2026 年已有约 50 万 Bank of Scotland 客户可问交易问题，官网视频称其为 Penny；消费洞察和储蓄建议是已披露重点。官方尚未证明 Penny 可直接转账、购理财、管卡或取消订阅。底层模型厂商、交易授权机制未公开。[21][22]

**Monzo。**2026 年 9 月公开了在英国 FCA 实测框架下的储蓄目标助手早期试验：LLM 负责解释与个性化，行动须接入可预测、可审计系统；用户保留决定权。2026 年 6 月的 Monzo Ops Agent 面向客服运营，不能算客户直接使用。全量上线时间、面客聊天入口、支付/投资/管卡执行及模型厂商均未证实；亮点是把不确定性、隐含假设和持续评测列入产品验证。[23][24]

**Starling。**2025 年 6 月的 **Spending Intelligence** 用 Google Gemini/Google Cloud 回答消费问题，隐私页说明模型不直接接触支付明细，由银行系统检索答案。2026 年 3 月 **Starling Assistant** 在英国个人 App 分批上线，支持自然语言及手机键盘语音输入，可创建储蓄 Spaces、安排内部资金转移/账单、查询直扣、锁卡；**任何账户操作都先请求用户批准**，且须主动选择启用，数据留在其 Google Cloud 环境、不用于训练。公开资料未确认向第三方付款的自动执行、理财申购或订阅商户解约。[25][26][27]

**bunq Finn。**2023 年 12 月推出，2025 年 12 月升级，覆盖 bunq App 支持/财务问答，官方称支持 38 种语言、约 90% 满意度，2025 年加入语音实时翻译；常问消费、卡、支付和储蓄。bunq 称采用自有 AI 系统，但未公开底层生成模型厂商、逐笔认证和由 Finn 直接发起转账/交易的证据。App 本身能支付，不应自动计入 Finn。[28]

### 3.3 拉美、亚太与跨地区

**Nubank。**2023 年 10 月在巴西 NuCommunity 小范围测试基于 GPT-4、专有模型与洞察工具的信用助手，解释额度、担保贷款和 NuPay 选择；Payment Assistant 则主动归集并提醒到期账单。没有可靠资料证明该试验已全量上线、经聊天完成贷款申领/转账或开放理财交易；两种“助手”也不应混算为单一 Agent。[32][33]

**Klarna AI assistant。**2024 年 2 月与 OpenAI 在 Klarna App 面向多个市场推出，处理购物、付款服务、退款和退货咨询，客户可转人工；公司披露首月覆盖约三分之二客服聊天。2026 年 Klarna Shopping Search 进入 ChatGPT，但商品搜索后仍跳转商家完成购买，不能算助手代扣或银行转账。客服 AI 底层为 OpenAI，交易确认与额度规则未公开。[39][40]

**Cleo AI。**2016 年创立，2025 年 7 月 Cleo 3.0 加入多步代理、记忆和实时语音；2026 年 1 月 Autopilot 在其 App 中推出目标规划、每日主动建议和用户确认后的储蓄转入、现金预支、商户支出限额。Cleo 官方同时写明更广泛的自主执行仍在未来路线图，当前每个 Actions 均须同意。其技术以专门子代理、结构化输出和交易增强为核心；模型品牌未完整披露。Cleo 是财务科技应用，不应等同持牌银行全套产品。[41][42]

**DBS digibot / Joy。**digibot 2016 年服务个人客户，Joy 2018 年进入企业银行；2024 年 Joy 融入 GenAI，2026 年 7 月 DBS 宣布两者服务新加坡、香港、台湾逾千万用户，个人 digibot 支持卡、退款、减免及汇款咨询，并计划 2026 年第四季度增加代理和语音能力。早期 POSB 机器人曾在 Facebook Messenger 回答产品问题。已证实的是咨询与引导，个人助手实际代办汇款、理财购买及强认证门槛未公开。[43][44]

**Commonwealth Bank Ceba。**2018 年 1 月在澳洲 NetBank 与 CommBank App 推出，可理解多种任务表述，公开示例有激活卡、查余额、付款、无卡取现，必要时转人工。2025 年 CommBank 与 OpenAI 战略合作，主要宣布员工工具、反欺诈与个性化探索；不能据此认定 Ceba 已改用 OpenAI 模型。App 内安全环境已披露，分级限额/认证细节未证实。[45][46]

**BBVA Blue。**2020 年 7 月在西班牙 App 推出自研 NLP/机器学习语音与文字助手，能查余额、消费、产品和导航；2025 年 BBVA 墨西哥新版 App 也加入 Blue。集团 2025 年提出以 OpenAI 合作推动 Blue 向多模态、预测需求和 24 小时交易演进，但战略路线图不等于现有对话交易已全部上线。客户投资申购、卡片挂失和代扣取消由 Blue 完成未证实。[47][48]

**Santander。**各地产品不同：英国虚拟助手公开称可处理复杂对话和**需要第二因素认证**的部分交易；智利 Santi 至少 2022 年已提供线上开户引导。集团 2025 年宣布与 OpenAI 合作推进 2026–27 年会话银行路线，但未给出统一全球面客 Agent 的上线日。2026 年 3 月与 Mastercard Agent Pay 在受控环境完成经真实支付设施处理的代理付款，按预设权限和限额执行；这是可行性验证，不能标为客户普遍可用。[49][50][51]

**ING。**德国 pING 在 2021 年已回答数百万客户问询，土耳其 INGo 更早涉及贷款审批；两者并非统一全球产品。2026 年 ING、Worldline、Mastercard 示范了客户选定音乐会门票并批准后由 AI 完成购买，官方明确目前仍属**受辅助流程**。没有公开证据说明普通客户已在 ING App 用通用助手跨六场景操作，底层模型和限额规则也未证实。[52][53]

**补充：Garanti BBVA Ugi。**土耳其 2016 年推出，2025 年引入 LLM/生成式 AI；2026 年官方称支持 300 多种端到端银行交易与每月约 78 万条主动通知。App 中 Ugi 与 WhatsApp/Facebook Messenger 的机器人共享部分服务知识，但跨渠道是否同权交易未证实。它说明大范围任务目录与主动触达已有市场先例，值得单列验证；公开材料没有逐项披露高风险交易审批细则。[54][55]

## 四、Agentic Payments 基础设施

**Google AP2。**2025 年 9 月发布的 Agent Payments Protocol 是代理、商户和支付方传递授权证据的开放协议，兼容卡、转账等支付方式。用户在场时，最初需求成为 **Intent Mandate** 的可审计上下文，查看商品、价格后再签 **Cart Mandate**；用户不在场时，事先签署含商户、价格上限、时间等条件的 Intent Mandate，代理在条件满足时生成 Cart Mandate。可验证凭证签名把“意图→购物车→支付方式”串成不可抵赖证据链。它提供授权表达，并不替代银行自身 KYC、反欺诈和实际支付认证。[34]

**OpenAI + Stripe ACP。**2025 年 9 月上线的 Agentic Commerce Protocol 让 ChatGPT 与商户交换商品、结算会话和订单状态；商户保留定价、承兑、履约与售后责任。首批 Instant Checkout 要用户明确确认购买、地址和支付细节。Delegated Payments/Stripe Shared Payment Token 以商户、金额上限和有效期约束一次支付，不把原始卡号交给代理；截至 2026 年仍需获批商户接入。它更像“会话内结算”，不代表可在无人值守时任意购物。[35][36]

**Visa Intelligent Commerce。**2025 年 4 月公布，将代理支付凭证令牌化；只有消费者可以指示何时激活支付凭证，结合身份认证、消费控制、实时信号与争议处理。2025 年 10 月的 Trusted Agent Protocol 让商户识别代理身份。2026 年的相关工具和 B2B 试点进一步验证受限代理付款，但商业开放程度依地区和伙伴而异。[37][56]

**Mastercard Agent Pay。**2025 年 4 月宣布 Agentic Tokens 与注册代理识别机制，将发卡行控制、支付通行密钥和令牌化接到代理购买流程；2026 年 Santander 的受控真实设施交易提供了端到端案例。**PayPal** 则支持 AP2，并以 Agent Ready/Braintree 对接 ACP 与 Google UCP，使商户沿用现有支付处理关系；其面向消费者的说明允许“逐次批准”或“预先授权”两种委托方式。[38][51][57][58]

**对银行业务可抽象出的通行做法：**（1）把自然语言目标转成明确的委托对象、业务范围、收款方、金额/日累计上限、时间窗和撤销条件；（2）支付前呈现最终受益人、商品/费用、金额和风险，用户在场时签署精确订单，离场时只执行事先签署的窄权限；（3）代理身份与用户身份分开认证，使用短时、限域令牌，不向模型泄露长期凭证；（4）金额、频率、商户和异常行为在执行层复核，超界转强验证或人工；（5）保存意图、版本化计划、审批、API 结果与回执，支持中断、幂等、补偿、争议与审计。前四项有协议/网络公开依据，最后一项是本项目据此提出的工程设计，不是所有竞品均已实施。[34][35][36][37][38]

## 五、能力矩阵表

图例：**●** 已公开证实完整办理该列主要任务；**◐** 只覆盖部分子任务、仅查询/导航或处于试点；**○** 官方明确不支持；**?** 未证实。矩阵中的“交易”指助手能引导完成真实资金/支付交易，内部储蓄转移也算部分；“强认证”要求公开说明逐笔确认或更高验证，单纯位于已登录 App 不算。App/IM/语音只表示该**助手**的渠道，键盘语音输入按“◐”处理。各行依据第三节对应来源，Revolut 行依据第二节来源。

| 产品 | 智能转账 | 账单分析 | 理财操作 | 卡片管理 | 订阅/代扣 | 跨场景联动 | 权限/强认证 | App | IM | 语音 | 主动服务 | 可执行交易 |
|---|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| Revolut AIR | ? | ● | ◐ | ● | ◐ | ? | ◐ | ● | ? | ? | ? | ◐ |
| BofA Erica | ◐ | ● | ◐ | ◐ | ◐ | ? | ? | ● | ? | ● | ● | ◐ |
| Capital One Eno | ? | ◐ | ? | ◐ | ◐ | ? | ? | ◐ | ● | ? | ● | ◐ |
| Wells Fargo Fargo | ? | ● | ? | ◐ | ? | ? | ? | ● | ? | ? | ◐ | ? |
| Chase Digital Assistant | ◐ | ◐ | ◐ | ● | ? | ? | ? | ● | ? | ? | ? | ◐ |
| Citi（面客产品） | ? | ◐ | ? | ◐ | ? | ? | ? | ? | ? | ◐ | ◐ | ? |
| U.S. Bank Smart Assistant | ● | ● | ? | ● | ◐ | ◐ | ◐ | ● | ? | ● | ◐ | ● |
| NatWest Cora+ | ? | ◐ | ? | ◐ | ? | ? | ? | ● | ? | ? | ◐ | ? |
| Lloyds Financial Assistant | ? | ◐ | ◐ | ? | ? | ? | ? | ● | ? | ? | ? | ? |
| Monzo 储蓄助手试验 | ? | ◐ | ? | ? | ? | ? | ◐ | ? | ? | ? | ? | ? |
| Starling Assistant | ◐ | ● | ? | ● | ◐ | ◐ | ◐ | ● | ? | ◐ | ◐ | ◐ |
| bunq Finn | ? | ● | ? | ◐ | ? | ? | ? | ● | ? | ◐ | ◐ | ? |
| Nubank 信用/支付助手 | ? | ◐ | ◐ | ◐ | ◐ | ? | ? | ◐ | ? | ? | ● | ? |
| Klarna AI assistant | ? | ◐ | ? | ? | ◐ | ? | ? | ● | ? | ? | ? | ? |
| Cleo Autopilot | ◐ | ● | ? | ◐ | ◐ | ◐ | ◐ | ● | ? | ● | ● | ◐ |
| DBS digibot/Joy | ? | ◐ | ◐ | ◐ | ? | ? | ? | ● | ◐ | ? | ◐ | ? |
| CommBank Ceba | ◐ | ◐ | ? | ● | ? | ? | ? | ● | ? | ? | ? | ◐ |
| BBVA Blue | ? | ● | ◐ | ? | ? | ? | ? | ● | ? | ● | ◐ | ? |
| Santander 各地助手 | ◐ | ? | ? | ◐ | ? | ? | ● | ◐ | ? | ? | ? | ◐ |
| ING pING/INGo | ? | ? | ? | ? | ? | ? | ? | ◐ | ? | ? | ? | ◐ |
| Garanti BBVA Ugi | ◐ | ◐ | ◐ | ◐ | ? | ◐ | ? | ● | ◐ | ◐ | ● | ● |

**读表限制：**“◐”不表示六场景全覆盖；例如 U.S. Bank 有自然语言转账但未见 AA 收款，Starling 的内部 Spaces 转移不是对外汇款，Santander/ING 的代理购买试验也未计作日常用户助手的完整能力。表中大量“?”是公开证据缺口，不等于产品绝无功能。综合看，账单洞察和卡片控制最普遍，理财执行、订阅商户侧解约和跨场景自主链路最少得到证实。[17][25][51][53]

## 六、对本项目的启示

1. **把六场景映射成有边界的工具目录。**每个动作定义输入、资金影响、审批级别、幂等键、补偿办法和回执字段。自然语言只负责理解和规划，银行核心系统负责事实与执行，借鉴 Starling 的“模型转译指令、系统查交易”隔离方式。[26][27]
2. **将“交易前摘要”做成统一确认卡。**显示收款人真实姓名/尾号、金额、手续费、时间、目的及日累计。模糊称呼、同名联系人、收款人新增时必须消歧；U.S. Bank 的金额/日期追问和确认闭环适合作为最小标准。[17][18]
3. **把绿黄红权限放在模型外。**绿色只读可自动执行；黄色必须显式确认，累计不超过 1000 元按赛题配置；红色触发银行现有多因子认证。实际限额由银行策略引擎按渠道、设备、收款人、欺诈风险动态调整，模型不得自行降低权限。[3][34][37]
4. **引入“委托书”表达跨场景目标。**生日采购应记录对象、日期、预算、商户白名单、配送信息和授权有效期；预留 1000 元与对外买花/蛋糕是不同权限。按 AP2 思路，用户先批准意图，最终购物车价格与商品仍需核对或落在事先明确的窄授权内。[34]
5. **用 DAG 展示过程与可中断点。**例如“识别生日→查询现金流→预留资金→查商户→生成订单→确认支付→回执提醒”。每节点展示证据和状态；失败时撤回未完成预留或标记需人工处理，禁止失败后静默重试扣款。该设计回应现有竞品对跨业务回退披露不足的缺口。[34][35]
6. **订阅管理分清三种操作。**识别重复扣款、停止银行卡/商户后续扣费、在服务商处取消订阅是不同结果。AIR 已公开前两类的一部分，比赛若要做“一键取消”，必须取得商户侧成功回执并解释剩余合同义务。[2][3]
7. **理财采用“信息—适当性—交易”三段流程。**先列产品事实与费用、风险等级、适合性依据，再提供比较；申购/赎回只走红色强认证，不让模型直接生成不可复核的收益承诺。AIR/Erica 以投资信息为主，留出可验证的交易闭环差异化空间。[3][6][10]
8. **主动服务要有触发证据和降噪机制。**订阅涨价、异常交易、生日事件和余额趋势须标注来源、置信度和可关闭规则；Erica 的大量主动洞察、Cleo 的日计划、Ugi 的通知证明主动触达有价值，但推送过度会削弱信任。[10][41][54]
9. **把防注入与幻觉控制落到执行链。**外部商户文本、账单备注、网页内容只能作数据，不能提升权限；所有工具参数做类型校验与收款方/金额复核。知识问答做检索溯源与拒答，交易以核心系统返回为准。J.P. Morgan Payments 的 RAG、追踪和幻觉评测可作为测试参考。[16]
10. **保留人工接管与证据链。**客户能查看原始意图、澄清、计划版本、授权、认证、执行结果；出错可暂停并转人工。NatWest 的欺诈分诊、Cora 的转人工及 AP2 的不可抵赖授权链均提示“可解释、可追责”应优先于代理自由度。[19][20][34]
11. **多渠道只共享计划，不共享默认交易权限。**App 可作为强认证主渠道；IM 适合提醒、查询与发起待确认请求；语音可输入意图，但支付审批须回到受信界面。Eno 的短信触达和 U.S. Bank 的语音确认展示了便利性，数字服务协议也提醒语音本身不是身份验证。[12][18]

**普遍短板：**公开资料里，多数产品仍以查询、客服、导航或有限账户操作为主；理财实盘交易、商户侧订阅取消、跨场景采购和可回退 DAG 缺少可核验证据。各厂商常公布“交互量”“处理率”，却很少给出按场景的真实成功率、误执行率、人工接管率或强认证触发率；宣传中的“agentic”需要用执行回执而非名称验证。[1][19][23][25][28]

## 七、参考来源

1. Revolut，2026-04-09，[AIR 发布新闻稿](https://www.revolut.com/news/revolut_enters_new_era_of_money_intelligence_with_launch_of_ai_assistant/)。
2. Revolut，[Getting started with AIR 帮助页](https://help.revolut.com/help/profile-and-plan/getting-started-with-air/)。
3. Revolut，2026-04-09，[AIR 产品博客（功能与生物识别）](https://www.revolut.com/blog/post/air-ai-by-revolut/)。
4. Revolut France，[2026-09 法国博客列表](https://www.revolut.com/en-FR/blog/)；[奥地利 AIR 页面](https://www.revolut.com/en-AT/air-ai-by-revolut/)。
5. Revolut，2026-06-26，[Revolut X 连接外部 AI](https://www.revolut.com/blog/post/revolut-x-connects-with-your-ai-assistant-uk/)。
6. Revolut，[AI Assistants 法律与隐私说明](https://www.revolut.com/legal/rita-disclaimer/)。
7. Payment & Banking，2026-09-30，[Revolut AIR 实测](https://paymentandbanking.com/revolut-air-im-praxistest-airnuchternd/)；[Reddit 个别用户反馈](https://www.reddit.com/r/Revolut/comments/1spgk4g/gb_air_access/)（非代表性样本）。
8. Revolut，2026-08-25，[Revolut Research 与 AIR 介绍](https://www.revolut.com/en-US/news/revolut_announces_launch_of_dedicated_ai_research_division_revolut_research/)。
9. Revolut，[AIR 逐步开放帮助说明](https://help.revolut.com/help/profile-and-plan/getting-started-with-air/)。
10. Bank of America，2025-08-20，[Erica 超 30 亿次交互](https://newsroom.bankofamerica.com/content/newsroom/press-releases/2025/08/a-decade-of-ai-innovation--bofa-s-virtual-assistant-erica-surpas.html)。
11. Bank of America，2024-04-08，[Erica 历年节点与订阅洞察](https://newsroom.bankofamerica.com/content/newsroom/press-releases/2024/04/bofa-s-erica-surpasses-2-billion-interactions--helping-42-millio.html)；[2023 年报](https://investor.bankofamerica.com/regulatory-and-other-filings/all-sec-filings/content/0000070858-24-000135/0000070858-24-000135.pdf)。
12. Capital One，2017-10-24，[Eno 短信助手](https://www.capitalone.com/learn-grow/money-management/eno-chatbot-banking-conversations-next-level/)；[Eno 当前功能](https://www.capitalone.com/digital/tools/eno/)。
13. Capital One，[Eno 虚拟卡与提醒](https://www.capitalone.com/about/newsroom/pay-safely/)。
14. Wells Fargo，2022-10-24，[Fargo 与 Google Cloud](https://newsroom.wf.com/news-releases/news-details/2022/Wells-Fargos-New-Virtual-Assistant-Fargo-to-Be-Powered-by-Google-Cloud-AI/)；[2024 股东代理说明](https://www.wellsfargo.com/assets/pdf/about/investor-relations/annual-reports/2024-proxy-statement.pdf)；[2025 年报](https://www.wellsfargo.com/assets/pdf/about/investor-relations/annual-reports/2025-annual-report.pdf)。
15. Chase，[Chase Digital Assistant 帮助页](https://www.chase.com/digital/customer-service/digital-assistant)。
16. J.P. Morgan Payments，2025-10-30，[Commerce Center 的生成式 AI 报表助手](https://developer.payments.jpmorgan.com/blog/product/virtual-assistant-ai-reporting)。
17. U.S. Bank，2020-07-23，[Smart Assistant 发布与转账演示](https://www.usbank.com/about-us-bank/company-blog/article-library/introducing-the-us-bank-smart-assistant.html)；[当前帮助页](https://www.usbank.com/customer-service/knowledge-base/KB0188866.html)。
18. U.S. Bank，2022-04-26，[西语语音助手](https://ir.usbank.com/news-events/news/news-details/2022/U.S.-Bank-Launches-Nations-First-Spanish-Language-Voice-Assistant-for-Banking-04-26-2022/default.aspx)；[数字服务协议](https://www.usbank.com/caas/digital-services-agreement.html)。
19. NatWest，2024-06，[Cora+ 与 IBM](https://www.natwestgroup.com/news-and-insights/news-room/press-releases/data-and-technology/2024/jun/natwest-launches-cora-plus-the-latest-generative-ai-upgrade-to-t.html)；2025-03，[与 OpenAI 合作](https://www.natwestgroup.com/news-and-insights/news-room/press-releases/ai-and-data/2025/mar/natwest-open-ai-collaborate-to-accelerate-cutting-edge-ai-transf.html)。
20. NatWest，2026-02，[Cora 内代理财务助手](https://www.natwestgroup.com/news-and-insights/latest-stories/ai-and-data/2026/feb/being-a-trusted-partner-for-tomorrows-banking-through-technology.html)；[消费洞察与欺诈分诊](https://www.natwestgroup.com/who-we-are/about-natwest-group/tech-data-and-AI/natwest-ai-adoption-report/how-we-use-AI-for-the-future-of-customer-relationships.html)。
21. Lloyds，2025-07-15，[Athena 员工工具](https://www.lloydsbankinggroup.com/media/press-releases/2025/lloyds-banking-group-2025/lloyds-accelerates-with-athena.html)。
22. Lloyds，2025-11，[客户 AI Financial Assistant 公告](https://www.lloydsbankinggroup.com/assets/pdfs/media/press-releases/2025-press-releases/lloyds-banking-group/251106-llg-unveils-uks-first-ai-powered-financial-assistant.pdf)；[2026 年 AI 页面](https://www.lloydsbankinggroup.com/who-we-are/group-overview/artificial-intelligence.html)。
23. Monzo，2026-09-21，[FCA AI Live Testing 储蓄助手](https://monzo.com/blog/fca-ai-live-testing-insights)。
24. Monzo，2026-06-04，[Monzo Ops Agent 员工运营工具](https://monzo.com/blog/engineering-the-future-of-customer-operations-the-monzo-ops-agent)。
25. Starling，2025-06-10，[Spending Intelligence](https://www.starlingbank.com/news/starling-launches-pioneering-ai-banking-tool/)。
26. Starling，2026-03-20，[Starling Assistant 发布](https://www.starlingbank.com/news/starling-launches-agentic-ai-assistant/)；[帮助页：操作前批准](https://help.starlingbank.com/joint/topics/using-the-app/what-is-starling-assistant-and-how-does-it-work/)。
27. Starling，[AI 隐私说明](https://www.starlingbank.com/legal/privacy-notice/ai-privacy/)。
28. bunq，2025-12-16，[Finn 升级、渠道与满意度](https://press.bunq.com/258930-bunq-launches-smarter-more-powerful-upgrade-to-genai-financial-assistant/)。
29. Citi，2020，[商业卡 Intelligent Virtual Agent](https://www.citigroup.com/global/news/press-release/2020/us-citi-commercial-cards-enhances-client-satisfaction-with-launch-of-intelligent-virtual-agent)。
30. Citi，2025-08-13，[CitiDirect Commercial Banking AI 功能](https://www.citigroup.com/global/news/press-release/2025/print/citi-global-roll-out-enhancements-citidirect-commercial-banking-platform)。
31. Citi，2026，[Services 投资者资料：CitiDirect AI Assistant](https://www.citigroup.com/rcs/citigpa/storage/public/investor-day/citi-ID26-services-presentation.pdf)。
32. Nubank，2023-10-18，[GPT-4 信用助手小范围试验](https://international.nubank.com.br/consumers/nubank-begins-testing-with-generative-artificial-intelligence-to-enhance-customers-experience-with-credit/)。
33. Nubank，2024-05-07，[AI 与 Payment Assistant](https://blog.nubank.com.br/nubank-artificial-intelligence-technology/)。
34. Google Cloud，2025-09，[AP2 公告与 Mandate 机制](https://cloud.google.com/blog/products/ai-machine-learning/announcing-agents-to-payments-ap2-protocol)。
35. OpenAI，2025-09-29，[Instant Checkout 与 ACP](https://openai.com/index/buy-it-in-chatgpt/)。
36. OpenAI Developers，[ACP Key Concepts 与 Delegated Payment](https://developers.openai.com/commerce/guides/key-concepts)。
37. Visa，2025-04-30，[Visa Intelligent Commerce](https://usa.visa.com/about-visa/newsroom/press-releases.releaseId.21361.html)；[开发者页面](https://developer.visa.com/use-cases/visa-intelligent-commerce-for-agents)。
38. Mastercard，2025-04-29，[Mastercard Agent Pay](https://www.mastercard.com/global/en/news-and-trends/press/2025/april/mastercard-unveils-agent-pay-pioneering-agentic-payments-technology-to-power-commerce-in-the-age-of-ai.html)。
39. Klarna，2024-02-27，[AI 客服助手首月数据](https://www.prnewswire.com/news-releases/klarna-ai-assistant-handles-two-thirds-of-customer-service-chats-in-its-first-month-302072740.html)；[SEC 申报](https://www.sec.gov/Archives/edgar/data/2003292/000200329225000024/klarnagroupplcf-1a3.htm)。
40. Klarna，2026-05-20，[Shopping Search 进入 ChatGPT](https://investors.klarna.com/News--Events/news/news-details/2026/Klarna-launches-AI-powered-Shopping-Search-app-in-ChatGPT/default.aspx)。
41. Cleo，2025-07-22，[Cleo 3.0](https://web.meetcleo.com/blog/Introducing-cleo-3-0)。
42. Cleo，2026-01-15，[Autopilot 技术与行动范围](https://web.meetcleo.com/blog/introducing-autopilot)。
43. DBS，2026-07-28，[digibot/Joy GenAI 与代理路线](https://www.dbs.com/newsroom/DBS_Gen_AI_enabled_virtual_assistants_reach_10_million_customers_and_go_agentic)。
44. DBS，2025-11，[Joy 从 NLU 到 LLM](https://www.dbs.com/artificial-intelligence-machine-learning/artificial-intelligence/singapore-finTech-festival-2025-powering-dbs-next-phase-of-growth-with-ai.html)；[POSB Messenger 机器人](https://www.dbs.com/newsroom/Chat_with_POSB_via_Facebook_Messenger_POSB_digibank_Virtual_Assistant)。
45. Commonwealth Bank，2018-01-19，[Ceba 上线](https://www.commbank.com.au/guidance/newsroom/cba-launches-chatbot-named-CEBA-201801.html)。
46. Commonwealth Bank，2025-08，[与 OpenAI 合作](https://www.commbank.com.au/articles/newsroom/2025/08/tech-ai-partnership.html)。
47. BBVA，2020-07-20，[Blue 西班牙上线](https://www.bbva.com/en/innovation/blue-bbvas-new-voice-assistant-in-spain/)；[墨西哥 2025 App](https://www.bbva.com/en/bbva-mexico-brings-personalization-and-zero-fees-to-the-digital-space/)。
48. BBVA，2025，[The Eight 与 Blue 路线图](https://www.bbva.com/en/innovation/bbva-deploys-the-eight-its-strategy-to-transform-the-financial-experience-with-ai/)。
49. Santander，[英国虚拟助手团队与二次认证能力](https://www.santander.com/en/careers/uk-careers/where-do-you-want-to-make-an-impact/digital)；[智利 Santi](https://www.santander.com/en/press-room/press-releases/2022/01/santander-chile-reaffirms-its-commitment-to-prospera-with-the-aim-of-helping-the-countrys-small-entrepreneur)。
50. Santander，2025，[与 OpenAI 合作的 2026–27 路线](https://www.santander.com/en/stories/santander-data-ai-first-strategy-accelerates-through-openai-collaboration)。
51. Santander，2026-03-02，[Mastercard Agent Pay 受控真实设施付款](https://www.santander.com/en/press-room/press-releases/2026/03/santander-and-mastercard-complete-europes-first-live-end-to-end-payment-executed-by-an-ai-agent)。
52. ING，[2021 年报：pING/INGo](https://ing.com/binaries/content/assets/documents/files/2021_annual_report_ing_bank_n.v.pdf)。
53. ING，2026-06-02，[与 Worldline/Mastercard 的代理支付示范](https://ing.com/news/2026/ai-can-now-shop-for-you-if-you-let-it.html)。
54. Garanti BBVA，2026-05-08，[Ugi 十周年与交易、主动通知数据](https://www.bbva.com/en/innovation/garanti-bbvas-smart-assistant-ugi-turns-10-ai-transforming-banking/)；[2025 GenAI 升级](https://www.bbva.com/en/innovation/garanti-bbva-enhances-its-smart-assistant-ugi-using-generative-artificial-intelligence/)。
55. Garanti BBVA，[Ugi 与 WhatsApp/Facebook Messenger 机器人](https://www.bbva.com/en/innovation/garanti-bbva-interacts-with-more-than-650000-customers-a-month-with-its-smart-assistant-ugi/)。
56. Visa，2025-10-14，[Trusted Agent Protocol](https://corporate.visa.com/en/sites/visa-perspectives/newsroom/visa-unveils-trusted-agent-protocol-for-ai-commerce.html)。
57. PayPal Developer，2026-06，[Agent Ready 支持 ACP/UCP](https://developer.paypal.com/agent-ready/overview/)；[PayPal 支持 AP2](https://developer.paypal.com/community/blog/PayPal-Agent-Payments-Protocol/)。
58. PayPal，2026-03-05，[消费者代理购物授权解释](https://www.paypal.com/us/money-hub/article/agentic-commerce-explained)。
