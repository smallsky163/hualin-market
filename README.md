1. 项目背景与目标
基于 V1.1 版本的自动化发布闭环，V1.2 旨在通过 Agent 语义理解、状态机管理和能量值模型，解决信息准确性、空间维度缺失以及 AI 调用成本失控的问题。
2. 核心架构：商品状态机 (Status Machine)
为了支撑交易闭环，商品生命周期由单一状态扩展为五阶段模型：
状态 (Status)
定义
触发条件
前端行为
DRAFT
草稿
图片上传，AI 初步生成
隐藏
ACTIVE
发布中
用户点击“确认上架”
公开展示
PENDING
交易中
买家发起意向，卖家确认
标记为“已锁定”
SOLD
已成交
确认收款
隐藏，进入评价
ARCHIVED
已下架
卖家手动撤回
隐藏

3. 交互逻辑：预览修正与空间补全
3.1 确认工作流
预览推送：Bot 输出 AI 生成的标题、价格和描述，并附带 Inline Buttons。
内容修正：
修改价格：点击按钮，用户回复新数字，Bot 异步更新数据库。
补充位置：Bot 提供快捷选项（如：北门、教学楼）或通过 Agent 提取用户回复的地点描述。
最终发布：扣除积分，状态转为 ACTIVE。
4. 计费体系：能量值 (Credit-based) 模型
目标：覆盖 Gemini Vision 模型的高额 Token 成本。
账户机制：profiles 表记录用户 credits 余额。
消耗规则：
多模态识图：-10 Credits/次（含智能估价）。
智能语义搜索：-1 Credit/次。
获取机制：
新用户：初始赠送 50 点。
每日留存：登录赠送 10 点。
5. 搜索升级：Agent 驱动的语义搜索
能力描述：支持自然语言复合查询（例：“搜索上海浦东 3000 元以内的电脑桌”）。
技术实现：
意图解析：Gemini 将模糊指令转化为结构化查询参数（JSON）。
精准过滤：后端根据 JSON 字段执行 Supabase 动态查询，替代传统关键字模糊匹配。
6. 信任体系与卖家面板 (Trust & Dashboard)
信用分 (Trust Score)：由 AI 定期审计聊天记录和成交时效，自动计算分值。
管理面板：用户通过 /manage 命令获取专属 Web 链接，实现商品的一键“擦亮”、下架和浏览量统计。
7. 数据库变更清单 (Database Schema)
表名
变更操作
字段名
类型
说明
items
新增
status
text
draft/active/sold/archived
items
新增
location
text
存储空间地理信息
items
新增
view_count
int4
网页端点击量统计
profiles
新建
credits
int4
用户剩余能量值
profiles
新建
trust_score
float4
初始分 80.0

