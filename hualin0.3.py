import requests
import io
import os
import telebot
import google.generativeai as genai
from telebot.types import MenuButtonWebApp, WebAppInfo
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from supabase import create_client, Client
import threading
from flask import Flask
from telebot import TeleBot
from telebot import types
import re
from datetime import date
import time
import random
#import Pillow
from PIL import Image
from concurrent.futures import ThreadPoolExecutor

ADMIN_ID = 7894972034  # 🌟 必须修改：你可以发消息给 @userinfobot 获取你的 ID

# 创建一个专门处理 AI 识图任务的线程池
executor = ThreadPoolExecutor(max_workers=10)

# 字符清洗
def escape_markdown(text):
    # Markdown (老版本) 只需要转义 * _ ` [
    if not text:
        return ""
    text = str(text)
    parse_chars = r'_*[]`' 
    return re.sub(f'([{re.escape(parse_chars)}])', r'\\\1', text)

# 1. 配置秘钥
GOOGLE_API_KEY = os.getenv("GEMINI_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY") # 记得用 service_role key


supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


# 2. 初始化 Gemini (使用你列表里确切的名字)
genai.configure(api_key=GOOGLE_API_KEY)
# 注意这里：一定要带 models/
model = genai.GenerativeModel('models/gemini-2.5-flash') 
chat = model.start_chat(history=[])

# 3. 初始化 Telegram
bot = telebot.TeleBot(TELEGRAM_TOKEN)

# --- 新增：给 Hugging Face 的保活逻辑 ---
app = Flask(__name__)

@app.route('/')
def health_check():
    return "Bot is running!"

def run_flask():
    # Hugging Face 默认使用 7860 端口
    app.run(host='0.0.0.0', port=7860)

# 设置 Bot 左下角的菜单按钮
try:
    bot.set_chat_menu_button(
        menu_button=MenuButtonWebApp(
            type="web_app",
            text="进入商城",
            web_app=WebAppInfo(url="https://smallsky163.github.io/hualin-market/") # 暂时用 bing 测试
        )
    )
    print("菜单按钮配置成功！")
except Exception as e:
    print(f"设置菜单按钮失败: {e}")

# 积分处理逻辑
def get_or_create_profile(user):
    # 尝试获取用户信息
    res = supabase.table("profiles").select("*").eq("telegram_id", user.id).execute()
    
    if not res.data:
        # 新用户，初始赠送 50 能量
        new_profile = {
            "telegram_id": user.id,
            "username": user.username or "未知邻居",
            "credits": 50
        }
        res = supabase.table("profiles").insert(new_profile).execute()
        return res.data[0]
    return res.data[0]

# 积分拦截与扣除

# 处理图片上传
def upload_to_supabase(file_id):
    try:
        # 1. 获取文件路径
        file_info = bot.get_file(file_id)
        file_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_info.file_path}"
        
        # 2. 下载原始图片
        response = requests.get(file_url)
        if response.status_code != 200:
            return None
            
        # --- 🚀 核心优化：Pillow 内存压缩 ---
        img_data = io.BytesIO(response.content)
        img = Image.open(img_data)
        
        # 统一缩放：宽度限制在 1280px（兼顾 Gemini 识别率与体积）
        if img.width > 1280:
            ratio = 1280 / float(img.width)
            new_height = int(float(img.height) * float(ratio))
            img = img.resize((1280, new_height), Image.Resampling.LANCZOS)
        
        # 转换为 JPEG 字节流并压缩质量至 75%
        output_buffer = io.BytesIO()
        if img.mode in ("RGBA", "P"): 
            img = img.convert("RGB")
        img.save(output_buffer, format="JPEG", quality=75, optimize=True)
        compressed_bits = output_buffer.getvalue()
        # --- 压缩结束 ---

        # 3. 生成唯一文件名
        file_name = f"{file_id}_{int(time.time())}.jpg"
        
        # 4. 上传至 Supabase
        supabase.storage.from_("item-images").upload(
            path=file_name,
            file=compressed_bits,
            file_options={"content-type": "image/jpeg"}
        )
        
        # 返回公网访问链接以及压缩后的字节流（用于后续给 AI，避免二次下载）
        public_url = supabase.storage.from_("item-images").get_public_url(file_name)
        return public_url, compressed_bits
    except Exception as e:
        print(f"I/O 链路异常: {e}")
        return None, None

# 处理广播逻辑 (增强版)
def notify_subscribers(item_id):
    try:
        # 1. 获取商品和卖家信息
        item = supabase.table("items").select("*").eq("id", item_id).single().execute().data
        if not item: return
        
        seller = supabase.table("profiles").select("trust_score").eq("telegram_id", item['telegram_id']).single().execute().data
        score = seller.get('trust_score', 0) if seller else 0
        
        # 2. 准备 HTML 格式的精美文案
        # 使用 <b> 替代 **，避免 Markdown 解析失败
        item_name = item['name'].replace('<','&lt;').replace('>','&gt;')
        
        # 根据分数显示星星数量
        stars = "⭐" * min(5, (score // 50 + 1)) # 每 50 分一颗星，最高5颗
        
        notification_html = (
            f"🔔 <b>【华邻捡漏】匹配到您的关注！</b>\n"
            f"━━━━━━━━━━━━━━\n"
            f"📦 <b>商品：</b> {item_name}\n"
            f"💰 <b>价格：</b> {item['price']} 刀\n"
            f"⭐️ <b>卖家信用：</b> {score} ({stars})\n"
            f"📍 <b>位置：</b> {item.get('location_text', '邻里中心')}\n"
            f"━━━━━━━━━━━━━━\n"
            f"💬 <b>描述：</b> {item['description'][:50]}...\n\n"
            f"👇 <i>点击下方按钮查看实拍大图或联系卖家</i>"
        )

        # 3. 构造 View 按钮
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔍 查看商品详情", callback_data=f"view_{item_id}"))

        # 4. 匹配并推送
        search_content = f"{item['name']} {item['description']}".lower()
        all_subs = supabase.table("subscriptions").select("*").execute()
        
        for sub in all_subs.data:
            if sub['keyword'].lower() in search_content and str(sub['telegram_id']) != str(item['telegram_id']):
                try:
                    bot.send_message(sub['telegram_id'], notification_html, 
                                     parse_mode="HTML", 
                                     reply_markup=markup)
                except Exception as e:
                    print(f"推送单条失败: {e}")
                    
    except Exception as e:
        print(f"推送逻辑全局异常: {e}")

# 汇总更新描述
# (Deleted duplicate function)


def get_latest_preview_text(item_id):
    # 1. 从数据库获取最新状态
    res = supabase.table("items").select("*").eq("id", item_id).single().execute()
    item = res.data
    if not item:
        return "⚠️ 数据丢失"

    # 2. 这里的 item['description'] 包含了 AI 最初生成的带价格的文案
    # 我们不直接删除它，而是通过拼接，让数据库的真实字段（price/location）成为“法官”
    
    # 🌟 重点：如果描述中包含 "DATA:" 这种原始标记，先切掉它
    clean_desc = item['description'].split("DATA:")[0].strip()

    # 3. 重新拼装文案：顶部显示绝对准确的“成交信息”
    # 使用 escape_markdown 并在 Legacy Markdown 中使用 *bold*
    text = (
        f"📋 *{escape_markdown(item['name'])}*\n"
        f"━━━━━━━━━━━━━━\n"
        f"💰 *当前价格：{escape_markdown(item['price'])}*\n" # 优先显示修改后的真实价格
        f"📍 *交易位置：{escape_markdown(item.get('location_text') or '未标注')}*\n"
        f"━━━━━━━━━━━━━━\n"
        f"📝 *宝贝详情：*\n"
        f"{escape_markdown(clean_desc)}\n\n" # 保留 AI 生成的建议和描述
        f"👤 卖家：@{escape_markdown(item.get('username', '未知'))}"
    )
    return text


def gen_draft_markup(item_id):
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(types.InlineKeyboardButton("✅ 确认发布", callback_data=f"conf_{item_id}"))
    markup.add(
        types.InlineKeyboardButton("💰 改价格", callback_data=f"editp_{item_id}"),
        types.InlineKeyboardButton("📝 改描述", callback_data=f"editd_{item_id}")
    )
    markup.add(
        types.InlineKeyboardButton("📍 加位置", callback_data=f"loc_{item_id}"),
        types.InlineKeyboardButton("❌ 撤回", callback_data=f"del_{item_id}")
    )
    return markup

# 处理价格修改逻辑
def update_price_logic(message, item_id, original_msg_id):
    new_price = message.text.strip()
    if new_price.isdigit():
        try:
            # 1. 更新数据库
            supabase.table("items").update({"price": new_price}).eq("id", item_id).execute()
            
            # 2. 获取最新合成文案
            new_text = get_latest_preview_text(item_id)
            
            # 3. 编辑原来的预览消息（关键步骤！）
            # 需要在 callback 触发时把预览消息的 message_id 传进来
            try:
                # 这里需要保留之前的按钮 markup
                bot.edit_message_text(
                    chat_id=message.chat.id,
                    message_id=original_msg_id,
                    text=f"🤖 **预览已更新！**\n\n{new_text}\n\n当前状态：⏳ 草稿",
                    parse_mode="Markdown",
                    reply_markup=gen_draft_markup(item_id) # 建议把按钮生成也封装成函数
                )
                bot.reply_to(message, "✅ 价格更新成功！")
            except Exception as e:
                print(f"刷新预览失败: {e}")
        except Exception as e:
            bot.reply_to(message, "❌ 修改失败，数据库连接异常。")
    else:
        bot.reply_to(message, "⚠️ 请输入纯数字，例如：88")

# 处理描述修改逻辑
# --- 建议放在 update_price_logic 附近 ---
def update_description_logic(message, item_id, original_msg_id):
    new_desc = message.text.strip()
    if len(new_desc) < 5:
        bot.reply_to(message, "⚠️ 描述太短啦，多写几个字让邻居更了解宝贝吧！")
        return
    
    try:
        # 更新数据库中的描述
        supabase.table("items").update({"description": new_desc}).eq("id", item_id).execute()     
        
        # 2. 🌟 关键：调用统一刷新函数
        new_text = get_latest_preview_text(item_id)
        # 净化文案用于预览
        safe_text = escape_markdown(new_text)

        # 3. 编辑原来的预览消息（关键步骤！）
        # 需要在 callback 触发时把预览消息的 message_id 传进来
        try:
            # 这里需要保留之前的按钮 markup
            bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=original_msg_id,
                text=f"🤖 **预览已更新！**\n\n{safe_text}\n\n当前状态：⏳ 草稿",
                parse_mode="Markdown",
                reply_markup=gen_draft_markup(item_id) # 建议把按钮生成也封装成函数
            )
            bot.reply_to(message, "✅ 描述更新成功！")
        except Exception as e:
            print(f"刷新预览失败: {e}")
    except Exception as e:
        print(f"修改描述失败: {e}")
        bot.reply_to(message, "❌ 修改失败，系统暂时无法连接数据库。")

# 处理位置输入逻辑
def update_location_logic(message, item_id, original_msg_id):
    loc_text = message.text.strip()
    try:
        supabase.table("items").update({"location_text": loc_text}).eq("id", item_id).execute()
        # 2. 获取最新合成文案
        new_text = get_latest_preview_text(item_id)
        
        # 3. 编辑原来的预览消息（关键步骤！）
        # 需要在 callback 触发时把预览消息的 message_id 传进来
        try:
            # 这里需要保留之前的按钮 markup
            bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=original_msg_id,
                text=f"🤖 **预览已更新！**\n\n{new_text}\n\n当前状态：⏳ 草稿",
                parse_mode="Markdown",
                reply_markup=gen_draft_markup(item_id) # 建议把按钮生成也封装成函数
            )
            bot.reply_to(message, "✅ 位置更新成功！")
        except Exception as e:
            print(f"刷新预览失败: {e}")
    except Exception as e:
        bot.reply_to(message, "❌ 位置保存失败。")

# 我的发布的处理逻辑
def handle_my_items_list(call):
    user_id = call.from_user.id
    bot.answer_callback_query(call.id)
    
    try:
        # 1. 修正 order 参数为 desc=True [根据报错反馈修正]
        res = supabase.table("items").select("*").eq("telegram_id", user_id).order("created_at", desc=True).execute()
        prof = supabase.table("profiles").select("trust_score").eq("telegram_id", user_id).single().execute()
        
        score = prof.data.get('trust_score', 0) if prof.data else 0
        
        if not res.data:
            bot.send_message(call.message.chat.id, "📭 您目前没有任何发布记录。")
            return

        # 2. 这里的标题部分使用 Markdown
        response_text = f"👤 **个人看板**\n⭐️ 华邻信用分：{score}\n━━━━━━━━━━━━━━\n"
        
        # 在循环内部
        for i, item in enumerate(res.data, 1):
            # 1. 价格格式化：去掉无意义的小数点，并加上单位
            raw_price = item.get('price', '0')
            try:
                # 将 "1000.0" 转换为 1000，如果是文字则保持不变
                price_num = float(raw_price)
                if price_num == int(price_num):
                    formatted_price = f"{int(price_num)}刀"
                else:
                    formatted_price = f"{price_num}刀"
            except (ValueError, TypeError):
                # 如果价格本身就是文字（如“面议”），则直接使用
                formatted_price = str(raw_price)

            # 2. 状态图标
            status = "✅在售" if item.get('status') == 'active' else "💰已售"
            
            # 3. 拼装（这里使用 rf 原始字符串解决你之前的语法警告）
            safe_name = escape_markdown(item.get('name', '未命名'))
            safe_price = escape_markdown(formatted_price)
            
            line = (
                rf"{i}\. *{safe_name}*" + "\n"
                rf"   价格：`{safe_price}` | {status}" + "\n"
                rf"   管理：/view\_{item['id']}" + "\n\n"
            )
            response_text += line

        # 3. 如果内容太长，分段发送或截断（Telegram 单条消息上限约 4000 字符）
        if len(response_text) > 4000:
            response_text = response_text[:3900] + "\n...(内容过多已截断)"

        bot.send_message(call.message.chat.id, response_text, parse_mode="Markdown")
        
    except Exception as e:
        print(f"获取记录失败: {e}")
        # 如果 Markdown 还是失败，作为保底方案，尝试用纯文本发送
        try:
            bot.send_message(call.message.chat.id, "⚠️ 记录中包含复杂格式，已切换纯文本显示：\n\n" + response_text.replace("*", "").replace("`", ""))
        except:
            pass

# 处理充值后的逻辑
def handle_admin_refill(call, data_parts):
    # data_parts 格式: ['refill', 'ok/no', 'user_id', 'amount', 'plan']
    sub_action = data_parts[1]
    target_user_id = data_parts[2]

    if sub_action == "no":
        bot.send_message(target_user_id, "❌ **充值审核未通过**\n您的支付凭证未通过核实。如有疑问，请联系管理员。")
        bot.edit_message_text(f"🗑️ 已拒绝用户 `{target_user_id}` 的申请。", call.message.chat.id, call.message.message_id)
        return

    # 处理 "ok" 逻辑
    if sub_action == "ok":
        amount = data_parts[3]
        plan = data_parts[4]
        
        from datetime import datetime, timedelta
        now = datetime.now()

        try:
            if plan == "credits":
                # --- 方案 A: 增加 100 能量 ---
                # 使用你已有的 increment_credits RPC
                supabase.rpc('increment_credits', {'user_id': int(target_user_id), 'amount': 100}).execute()
                res_text = "100 能量 (⚡)"
            
            if plan == "monthly":
                # 增加 31 天，并转为符合 Postgres 要求的字符串格式
                expiry_date = (now + timedelta(days=31)).strftime('%Y-%m-%d %H:%M:%S')
                supabase.table("profiles").update({"subscription_expiry": expiry_date}).eq("telegram_id", target_user_id).execute()
                res_text = "月度会员 (31天)"

            elif plan == "yearly":
                # 增加 365 天
                expiry_date = (now + timedelta(days=365)).strftime('%Y-%m-%d %H:%M:%S')
                supabase.table("profiles").update({"subscription_expiry": expiry_date}).eq("telegram_id", target_user_id).execute()
                res_text = "年度会员 (365天)"

            # 通知用户
            bot.send_message(target_user_id, f"🎉 **充值审核通过！**\n您的【{res_text}】已成功到账，感谢支持！")
            
            # 更新管理员界面状态
            bot.edit_message_text(f"✅ 已成功为用户 `{target_user_id}` 办理 {res_text}。", 
                                 call.message.chat.id, call.message.message_id, parse_mode="Markdown")

        except Exception as e:
            print(f"充值审批执行失败: {e}")
            bot.answer_callback_query(call.id, "❌ 数据库更新失败", show_alert=True)

# 回到欢迎页
def get_start_keyboard():
    """封装主页按钮逻辑，方便多处复用"""
    markup = types.InlineKeyboardMarkup(row_width=2)
    btn_help = types.InlineKeyboardButton("❓ 帮助中心", callback_data="help_main")
    btn_me = types.InlineKeyboardButton("👤 个人中心", callback_data="my_items")
    btn_recharge = types.InlineKeyboardButton("⚡ 获取能量", callback_data="recharge_menu")
    
    markup.add(btn_help)
    markup.add(btn_me, btn_recharge)
    return markup




@bot.callback_query_handler(func=lambda call: True)
def callback_inline(call):
    # 解析 callback_data，例如 "conf_123" -> action="conf", item_id="123"
    try:
        data_parts = call.data.split('_')
        action = data_parts[0]
        
        # --- 分支 A: 处理充值套餐 (匹配 recharge_xxx) ---
               
        if action == "recharge" and len(data_parts) == 2 and data_parts[1] == "menu":
            bot.answer_callback_query(call.id)
            
            # 🌟 直接构造和 recharge_command 一样的键盘和文案
            markup = types.InlineKeyboardMarkup(row_width=1)
            markup.add(
                types.InlineKeyboardButton("🔋 100 能量包 (10元)", callback_data="recharge_10_credits"),
                types.InlineKeyboardButton("💎 月度会员 (50元)", callback_data="recharge_50_monthly"),
                types.InlineKeyboardButton("🔥 年度会员 (99元)", callback_data="recharge_99_yearly"),
                types.InlineKeyboardButton("🔙 返回主菜单", callback_data="back_to_start")
            )
            
            recharge_text = "⚡ **充值中心**\n请选择适合您的套餐："
            bot.edit_message_text(recharge_text, call.message.chat.id, call.message.message_id, reply_markup=markup)
            return

        if action == "recharge":
            amount = data_parts[1]
            bot.answer_callback_query(call.id)
            pay_msg = (
                f"💳 **确认充值方案**\n"
                f"━━━━━━━━━━━━━━\n"
                f"金额：{amount} 刀\n"
                f"备注 ID：`{call.from_user.id}`\n\n"
                f"请扫码支付后，**直接在此发送支付截图**。\n"
                f"管理员核实后将立即到账。"
            )
            bot.send_message(call.message.chat.id, pay_msg, parse_mode="Markdown")
            return
            
        # --- 分支 B: 处理我的发布 (匹配 my_items) ---
        if action == "my" and "items" in data_parts:
            # 这里的匹配逻辑对应按钮的 "my_items"
            bot.answer_callback_query(call.id)
            # 🌟 这里的逻辑应该和你的 me_command(message) 函数内容保持高度一致
            # 获取用户信用和发布记录
            user_id = call.from_user.id
            profile = supabase.table("profiles").select("*").eq("telegram_id", user_id).single().execute().data
            
            credits = profile.get('credits', 0) if profile else 10
            score = profile.get('trust_score', 0) if profile else 10
            
            # 构造个人看板文案
            me_text = (
                f"👤 **个人中心**\n"
                f"━━━━━━━━━━━━━━\n"
                f"⚡ 剩余能量：{credits}\n"
                f"⭐ 信用积分：{score}\n"
                f"━━━━━━━━━━━━━━\n"
                f"以下是您的发布记录："
            )
            # 或者直接在这里编辑消息
            bot.edit_message_text(me_text, call.message.chat.id, call.message.message_id, parse_mode="Markdown") 
            handle_my_items_list(call)
            return
        
        # --- 分支 C: 处理管理员审批 (匹配 refill_xxx) ---
        if action == "refill":
            # 保持你现有的 refill_ok/no 逻辑，但注意参数下标
            handle_admin_refill(call, data_parts)
            return

        # --- 分支 C: 处理通知的view按钮 ---
        if action == "view":
            item_id = data_parts[1]
            bot.answer_callback_query(call.id)
            
            item = supabase.table("items").select("*").eq("id", item_id).single().execute().data
            if not item:
                bot.send_message(call.message.chat.id, "❌ 该商品已下架或被删除。")
                return
                
            # 再次获取最新的信用分
            seller = supabase.table("profiles").select("trust_score").eq("telegram_id", item['telegram_id']).single().execute().data
            score = seller.get('trust_score', 0) if seller else 0

            # 2. 这里的核心修复：对 HTML 特殊字符进行转义，防止描述里的 < > 导致解析失败
            safe_name = item['name'].replace('<','&lt;').replace('>','&gt;')
            safe_desc = item['description'].replace('<','&lt;').replace('>','&gt;')
            
            # 3. 构造 HTML 格式文案（使用 <b> <b> 代替 ** **)
            detail_text = (
                f"📋 <b>商品详情预览</b>\n"
                f"━━━━━━━━━━━━━━\n"
                f"🏷 <b>名称：</b> {safe_name}\n"
                f"💰 <b>标价：</b> {item['price']} 刀ss\n"
                f"📍 <b>地点：</b> {item.get('location_text', '未知')}\n"
                f"━━━━━━━━━━━━━━\n"
                f"📝 <b>描述：</b>\n{safe_desc}\n"
            )

            markup = types.InlineKeyboardMarkup()
            # 按钮 1：直接联系
            contact_url = f"https://t.me/{item['username']}" if item.get('username') else f"tg://user?id={item['telegram_id']}"
            markup.add(types.InlineKeyboardButton("💬 立即私聊卖家", url=contact_url))
            
            # 按钮 2：如果是卖家自己，增加一个“管理”选项
            if str(call.from_user.id) == str(item['telegram_id']):
                markup.add(types.InlineKeyboardButton("⚙️ 我要修改/下架", callback_data=f"my_items"))

            # 🌟 如果有图，发送照片详情；没图则发文字
            try:
                if item.get("image_url"):
                    bot.send_photo(
                        call.message.chat.id, 
                        item["image_url"], 
                        caption=detail_text, 
                        parse_mode="HTML", 
                        reply_markup=markup
                    )
                else:
                    bot.send_message(
                        call.message.chat.id, 
                        detail_text, 
                        parse_mode="HTML", 
                        reply_markup=markup
                    )
            except Exception as e:
                print(f"发送详情失败: {e}")
                # 如果 HTML 也解析失败（极端情况），回退到纯文本发送
                bot.send_message(call.message.chat.id, f"📦 商品：{safe_name}\n价格：{item['price']}\n描述：{safe_desc}", reply_markup=markup)    
        # 在 callback_inline 的 action 分类中增加
        if action == "help":
            bot.answer_callback_query(call.id)
            help_detail = (
                "📖 **华邻易市 · 指南针**\n\n"
                "🟢 **发布技巧**\n"
                "• 直接发送照片即可开始 AI 识别。\n"
                "• 识别后点击【改价】或【改描述】可微调内容。\n"
                "• 确认发布后，宝贝将进入全社区信息流。\n\n"
                "🔵 **买家必看**\n"
                "• /sub `关键词`：开启捡漏雷达。\n"
                "• 点击通知中的【查看详情】可直接私聊卖家。\n\n"
                "🟡 **账户相关**\n"
                "• /me：查看您的信用分、发布记录和会员状态。\n"
                "• /recharge：获取更多识图能量或开通会员。\n\n"
                "• /sign：每日签到获取5点能量。\n\n"
                "📖 **华邻易市 · 规则说明书**\n\n"
                "⚡ **能量点 (Credits)**\n"
                "• **消耗**：每次使用 AI 识图识别照片消耗 1 点。\n"
                "• **获取**：每日签到、参与社区活动或通过 /recharge 充值。\n"
                "• **特权**：月度/年度会员在有效期内识图不消耗能量。\n\n"
                "⭐ **信用分 (Trust Score)**\n"
                "• **初始**：新用户默认 10 分。\n"
                "• **奖励**：每成功卖出一件宝贝并标记已售，信用 + 10 分。\n"
                "• **作用**：信用分是邻里信任的基石。高分卖家的商品会有专属【优质】标识。\n\n"
                "━━━━━━━━━━━━━━\n"
                "🤝 **交易建议**\n"
                "本平台仅提供信息撮合，请大家在公共区域面交，检查实物后再付款哦！"
            )
            
            # 增加一个返回主菜单的按钮
            back_markup = types.InlineKeyboardMarkup()
            back_markup.add(types.InlineKeyboardButton("🔙 返回欢迎页", callback_data="back_to_start"))
            
            bot.edit_message_text(help_detail, call.message.chat.id, call.message.message_id, 
                                parse_mode="Markdown", reply_markup=back_markup)
            return

        # 增加返回逻辑
        # 处理“返回主菜单”按钮
        if action == "back" and data_parts[1] == "to" and data_parts[2] == "start":
            bot.answer_callback_query(call.id)
            
            # 修改回初始欢迎文案
            welcome_back_text = "🌟 **欢迎回到华邻易市主菜单**\n请选择您要执行的操作："
            
            bot.edit_message_text(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                text=welcome_back_text,
                reply_markup=get_start_keyboard(), # 🌟 使用刚才定义的函数
                parse_mode="Markdown"
            )
            return

        # --- 第二类：需要 item_id 的动作 ---
        if len(data_parts) < 2:
            return
            
        item_id = data_parts[1]

        if action == "conf":
            user_id = call.from_user.id
            username = call.from_user.username # 获取当前点击确认的人的用户名

            if not username:
                # 如果没有用户名，弹出强力提醒（买家将无法通过网页联系他）
                bot.answer_callback_query(
                    call.id, 
                    "⚠️ 你没有设置 Telegram 用户名！\n邻居在商城将无法直接联系你。\n请在 TG 设置中配置 Username 后再试。", 
                    show_alert=True
                )
                # 这里可以选择是否拦截发布。建议拦截，直到他设置好。
                return 

            # 如果有用户名，则更新数据库：状态改为 active，并存入用户名
            res = supabase.table("items").update({
                "status": "active",
                "username": username
            }).eq("id", item_id).execute()
            
            if res.data:
                bot.edit_message_text(
                    f"✅ 发布成功！\n邻居现在可以通过 @{call.from_user.username} 联系你啦。", 
                    call.message.chat.id, 
                    call.message.message_id
                )
                
                # 🌟 在这里调用广播函数
                notify_subscribers(item_id)
            
        elif action == "editp":
            msg = bot.send_message(call.message.chat.id, "💰 请回复新的价格（仅限数字）：")
            bot.register_next_step_handler(msg, update_price_logic, item_id, call.message.message_id)
        # --- 在 callback_inline 函数中添加分支 ---
        elif action == "editd":
            # 1. 获取当前旧描述
            item_res = supabase.table("items").select("description").eq("id", item_id).single().execute()
            old_desc = item_res.data.get('description', '') if item_res.data else ""

            # 2. 构造提示消息
            # 我们使用 MarkdownV2 的等宽字体块，它对 Emoji 的兼容性比 HTML code 标签更好一些
            # 注意：我们需要对旧描述进行转义，防止特殊字符导致发送失败
            safe_old_desc = escape_markdown(old_desc)
            
            instruction = (
                "📝 *进入描述编辑模式*\n\n"
                "*当前描述（点击下方文字自动复制）：*\n"
                f"`{safe_old_desc}`\n\n"
                "粘贴后修改部分文字再发送给我即可。"
            )
            
            try:
                msg = bot.send_message(
                    call.message.chat.id, 
                    instruction, 
                    parse_mode="MarkdownV2" 
                )
                bot.register_next_step_handler(msg, update_description_logic, item_id, call.message.message_id)
            except Exception as e:
                # 如果 MarkdownV2 还是因为特殊 Emoji 报错，回退到最稳健的普通文本
                print(f"Mv2发送失败，回退模式: {e}")
                msg = bot.send_message(call.message.chat.id, f"📝 请回复新的描述。原内容如下，请手动复制：\n\n{old_desc}")
                bot.register_next_step_handler(msg, update_description_logic, item_id, call.message.message_id)

        elif action == "loc":
            #msg = bot.send_message(call.message.chat.id, "📍 请回复交易位置（如：南门、学5楼）：")
            #bot.register_next_step_handler(msg, update_location_logic, item_id)
            # 创建一个回复键盘（Reply Keyboard），它会出现在打字区域
            markup = types.ReplyKeyboardMarkup(one_time_keyboard=True, resize_keyboard=True)
            # 这是一个特殊按钮，点击后会自动弹出手机系统位置请求
            btn_loc_request = types.KeyboardButton("📍 点击发送我的当前位置", request_location=True)
            markup.add(btn_loc_request)
            
            msg = bot.send_message(
                call.message.chat.id, 
                "请点击下方按钮发送当前位置，或者直接在这里输入文字地点：", 
                reply_markup=markup
            )
            # 记录下这个 item_id，方便一会儿处理收到的位置
            bot.register_next_step_handler(msg, handle_location_input, item_id, call.message.message_id)

        elif action == "del":
            supabase.table("items").delete().eq("id", item_id).execute()
            bot.edit_message_text("🗑️ 已删除该草稿。", call.message.chat.id, call.message.message_id)
        # --- 在 callback_inline 函数中添加以下逻辑 ---
        elif action == "sold":
            try:
                # 1. 更新商品状态为已售
                supabase.table("items").update({"status": "sold"}).eq("id", item_id).execute()
                
                # 2. 增加信用积分 (profiles 表)
                user_id = call.from_user.id
                profile_res = supabase.table("profiles").select("trust_score").eq("telegram_id", user_id).execute()
                
                new_score = 10 # 默认加 10 分
                if profile_res.data:
                    current_score = profile_res.data[0].get('trust_score') or 0
                    new_score = current_score + 10
                    supabase.table("profiles").update({"trust_score": new_score}).eq("telegram_id", user_id).execute()
                
                # 3. 彻底刷新预览消息：移除所有按钮，替换为成交文案
                # 获取商品标题用于展示
                item_data = supabase.table("items").select("name").eq("id", item_id).single().execute().data
                item_name = item_data.get('name', '该宝贝') if item_data else "该宝贝"
                
                bot.edit_message_text(
                    chat_id=call.message.chat.id,
                    message_id=call.message.message_id,
                    text=f"🎉 **恭喜成交！**\n\n【{item_name}】已标记为已售。\n您的卖家信用分 +10 (当前总分: {new_score})。\n\n*温馨提示：高信用分的卖家在搜索中会更靠前哦！*",
                    parse_mode="Markdown"
                )
                
                # 4. 可选：向频道/订阅者发送“已售”通知（根据您的业务需求决定是否开启）
                # notify_sold_status(item_id) 
                
                bot.answer_callback_query(call.id, "✅ 标记成功，信用分已入账！")
                
            except Exception as e:
                print(f"标记已售操作失败: {e}")
                bot.answer_callback_query(call.id, "⚠️ 操作失败，请联系管理员")
        # --- 在 callback_inline 处理 action 的 elif 链中添加 ---
        elif action == "recharge":
            amount = data_parts[1]
            bot.answer_callback_query(call.id)
            
            # 设置不同金额对应的文案
            package_name = {
                "10": "100 能量 (基础套餐)",
                "50": "月度会员 (无限识图)",
                "99": "年度会员 (超级邻居)"
            }.get(amount, "未知套餐")

            recharge_msg = (
                f"💳 **您选择了：{package_name}**\n\n"
                f"━━━━━━━━━━━━━━\n"
                f"1. 请扫描下方二维码支付 **{amount} 刀（根据实时汇率换算即可）**\n"
                f"2. 支付成功后，**请务必发送“支付截图”** 给本机器人\n"
                f"3. 管理员审核通过后，能量将自动到账\n"
                f"━━━━━━━━━━━━━━\n"
                f"👇 请直接在此对话框发送截图"
            )
            # 发送支付指引（这里可以带一张收款码图片）
            bot.send_message(call.message.chat.id, recharge_msg, parse_mode="Markdown")
            return
        
    except Exception as e:
        print(f"Callback 运行异常: {e}")
        bot.answer_callback_query(call.id, "❌ 操作解析失败")


@bot.message_handler(commands=['start'])
def send_welcome(message):
    profile = get_or_create_profile(message.from_user)
    welcome_text = (
        f"🌟 **欢迎来到华邻易市，{profile['username']}！**\n\n"
        "我是您的 AI 邻里二手助手。在这里，买卖闲置变得前所未有的简单：\n\n"
        "📸 **想卖宝贝？**\n"
        "只需直接发给我一张**商品照片**，AI 会自动帮您识别名称、价格并生成描述。\n\n"
        "🔍 **想捡漏？**\n"
        "使用 /sub 设置关键词（如：自行车），有邻居发布时我会立即通知您。\n\n"
        "━━━━━━━━━━━━━━\n"
        "🎁 **您的新手大礼包已到账：**\n"
        "• ⚡ **10 初始能量**：可免费识图发布 10 次宝贝\n"
        "• ⭐ **10 初始信用**：良好的开端是成交的一半\n\n"
        "━━━━━━━━━━━━━━\n"
        "💡 **它们有什么用？**\n"
        "• **能量**：AI 识图就像聘请了一位专业鉴定师，每次识别会消耗 1 点能量。\n"
        "• **信用**：信用分越高，您的宝贝在通知列表里排名越靠前，买家更放心！\n\n"
        "📸 **现在就发一张照片试试吧？**"
        "👇 **点击下方按钮探索更多功能**"
    )

    markup = types.InlineKeyboardMarkup(row_width=2)
    btn_help = types.InlineKeyboardButton("❓ 帮助中心", callback_data="help_main")
    btn_me = types.InlineKeyboardButton("👤 个人中心", callback_data="my_items")
    btn_recharge = types.InlineKeyboardButton("⚡ 充值能量", callback_data="recharge_menu") # 关联到之前的充值逻辑
    
    markup.add(btn_help)
    markup.add(btn_me, btn_recharge)

    bot.send_message(message.chat.id, welcome_text, reply_markup=markup, parse_mode="Markdown")

@bot.message_handler(commands=['me', 'my'])
def handle_my_info(message):
    user_id = message.from_user.id
    
    try:
        # 从 profiles 查数据
        res = supabase.table("profiles").select("*").eq("telegram_id", user_id).execute()
        
        # 兜底：如果数据库没这人，说明是新用户
        if not res.data:
            display_name = message.from_user.first_name or "宝藏邻居"
            trust_score = 0
            credits = 0
            expiry = "尚未开通"
        else:
            profile = res.data[0]
            # 解决“未知邻居”：优先用 TG 名字，其次用表里存的 username
            display_name = message.from_user.first_name or profile.get('username') or "宝藏邻居"
            trust_score = profile.get('trust_score', 0)
            credits = profile.get('credits', 0)
            expiry = profile.get('subscription_expiry') or "尚未开通"

        text = (
            f"👤 **个人中心**\n"
            f"━━━━━━━━━━━━━━\n"
            f"🏷 昵称：{display_name}\n"
            f"⭐️ 信用分：{trust_score}\n"
            f"💰 能量值：{credits}\n"
            f"📅 会员到期：{expiry}\n"
            f"━━━━━━━━━━━━━━\n"
            f"💡 *温馨提示：点击下方按钮查看或管理您的发布。*"
        )
        
        # 按钮保持原有逻辑（查看我的发布等）
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("📦 我的发布记录", callback_data="my_items"))
        bot.reply_to(message, text, reply_markup=markup, parse_mode="Markdown")
        
    except Exception as e:
        print(f"获取个人中心失败: {e}")
        bot.reply_to(message, "⚠️ 无法读取个人资料，请稍后再试。")

# 0.4.1 将模糊的搜索词转化为结构化的 SQL 查询条件。
def parse_search_query(user_text):
    search_prompt = f"""
    你是一个二手交易平台的搜索助手。请从用户的输入中提取结构化搜索条件。
    用户输入："{user_text}"
    
    请严格输出以下 JSON 格式（不要有任何额外文字）：
    {{
      "keyword": "提取的商品核心词",
      "max_price": "提取的价格上限，若无则为 null",
      "location": "提取的地名/校区，若无则为 null"
    }}
    """
    try:
        response = model.generate_content(search_prompt)
        # 提取并解析 JSON
        import json
        # 有时 AI 会带 ```json 标签，需要清理
        clean_json = response.text.replace('```json', '').replace('```', '').strip()
        return json.loads(clean_json)
    except Exception as e:
        print(f"Agent 解析搜索失败: {e}")
        return None
# 0.4.2实现搜索指令逻辑
@bot.message_handler(commands=['search'])
def handle_smart_search(message):
    query_text = message.text.replace('/search', '').strip()
    if not query_text:
        bot.reply_to(message, "🔍 请在指令后输入搜索内容，例如：\n`/search 100块以内的杯子`", parse_mode="Markdown")
        return

    # 1. 检查积分（智能搜索消耗 1 能量）
    profile = get_or_create_profile(message.from_user)
    if profile['credits'] < 1:
        bot.reply_to(message, "❌ 能量不足，无法进行智能搜索。")
        return

    bot.send_chat_action(message.chat.id, 'typing')
    
    # 2. 调用 Agent 解析意图
    criteria = parse_search_query(query_text)
    if not criteria:
        bot.reply_to(message, "😵 AI 没听懂你的搜索需求，请换个说法。")
        return

    # 3. 构造数据库查询
    query = supabase.table("items").select("*").eq("status", "active")
    
    # 3. 构造数据库查询 (升级版)
    # 使用 or 逻辑：匹配标题 或者 匹配描述
    if criteria.get('keyword'):
        k = f"%{criteria['keyword']}%"
        # Supabase 的 or 语法：.or_("name.ilike.%key%,description.ilike.%key%")
        query = query.or_(f"name.ilike.{k},description.ilike.{k}")
    
    if criteria.get('max_price'):
        query = query.lte("price", float(criteria['max_price']))
    
    if criteria.get('location'):
        query = query.ilike("location_text", f"%{criteria['location']}%")

    res = query.execute()

    # 4. 扣除 1 能量并反馈结果
    supabase.table("profiles").update({"credits": profile['credits'] - 1}).eq("telegram_id", message.from_user.id).execute()
    
    if not res.data:
        bot.reply_to(message, f"😿 没找到符合条件【{query_text}】的宝贝呢。")
    else:
        results_text = "🔎 **为您找到以下宝贝：**\n\n"
        for item in res.data[:5]: # 仅显示前5个
            seller_id = item.get('telegram_id')
            # 构造一个直接拉起私聊的链接
            # 注意：tg://user?id= 仅在手机端点对点生效，t.me/ 则更通用
            contact_url = f"tg://user?id={seller_id}"
            
            results_text += (
                f"📦 **{item['name']}**\n"
                f"💰 价格：{item['price']}\n"
                f"📍 位置：{item.get('location_text') or '未标注'}\n"
                f"👤 [点击这里联系卖家]({contact_url})\n"
                f"━━━━━━━━━━━━━━\n"
            )
        bot.reply_to(message, results_text, parse_mode="Markdown")

# 0.4.3 实现显示我的货架_old
#@bot.message_handler(commands=['my'])
# def list_my_items(message):
#     # 查询当前用户发布的 active 商品
#     res = supabase.table("items").select("*").eq("telegram_id", message.from_user.id).eq("status", "active").execute()
#     
#     if not res.data:
#         bot.reply_to(message, "📭 你目前没有正在售卖的宝贝哦。发送照片开启第一单吧！")
#         return
# 
#     for item in res.data:
#         markup = types.InlineKeyboardMarkup()
#         # 增加标记已售按钮
#         btn_sold = types.InlineKeyboardButton("🤝 标记为已售", callback_data=f"sold_{item['id']}")
#         btn_del = types.InlineKeyboardButton("🗑️ 删除下架", callback_data=f"del_{item['id']}")
#         markup.add(btn_sold, btn_del)
#         
#         bot.send_message(
#             message.chat.id, 
#             f"📦 **商品：{item['name']}**\n💰 价格：{item['price']}\n📅 发布时间：{item['created_at'][:10]}", 
#             reply_markup=markup
#         )
# 0.4.3.1 处理用户发送的位置信息经纬度翻译
def gemini_reverse_geocoding(lat, lon):
    prompt = f"""
    你是一个地理信息专家。我给你一个坐标：纬度 {lat}, 经度 {lon}。
    请根据这个坐标，告诉该位置所在的：国家、城市、区域（或街道/著名地标）。
    要求：
    1. 语言使用中文。
    2. 只输出具体地址，不要有任何多余的解释。
    例如：美国纽约曼哈顿第五大道。
    """
    try:
        # 使用你代码里已有的 model 对象
        response = model.generate_content(prompt)
        return response.text.strip()
    except Exception as e:
        print(f"Gemini 地址转换失败: {e}")
        return f"坐标 ({lat:.3f}, {lon:.3f})"

# 0.4.3.2 处理收到的地理位置数据
def handle_location_input_old(message, item_id, original_msg_id):
    if message.location:
        lat = message.location.latitude
        lon = message.location.longitude
        
        # 🌟 进度反馈
        bot.send_chat_action(message.chat.id, 'find_location')
        
        # 🌟 让 Gemini 翻译经纬度为人类读得懂的地名
        readable_address = gemini_reverse_geocoding(lat, lon)
        
        # 更新到数据库
        supabase.table("items").update({"location_text": readable_address}).eq("id", item_id).execute()
        
        bot.reply_to(
            message, 
            f"📍 自动定位：{readable_address}\n\n信息已补全，点击“确认发布”即可上架。", 
            reply_markup=types.ReplyKeyboardRemove()
        )
    else:
        # 处理文字输入
        loc_text = message.text.strip()
        supabase.table("items").update({"location_text": loc_text}).eq("id", item_id).execute()
        bot.reply_to(message, f"✅ 位置已更新为：{loc_text}", reply_markup=types.ReplyKeyboardRemove())

def handle_location_input(message, item_id, original_msg_id):
    readable_address = ""
    
    # 情况 A：用户通过按钮发送了地理位置坐标
    if message.location:
        lat = message.location.latitude
        lon = message.location.longitude
        bot.send_chat_action(message.chat.id, 'find_location')
        # 调用您已有的 Gemini 逆地理编码函数
        readable_address = gemini_reverse_geocoding(lat, lon)
    # 情况 B：用户直接回复了文字地点
    else:
        readable_address = message.text.strip()

    if not readable_address:
        bot.reply_to(message, "⚠️ 未能识别位置，请重新输入。")
        return

    try:
        # 1. 更新数据库中的位置字段
        supabase.table("items").update({"location_text": readable_address}).eq("id", item_id).execute()
        
        # 2. 获取包含最新位置、价格、描述的完整文案
        new_text = get_latest_preview_text(item_id)
        
        # 3. 核心：编辑原来的预览消息
        bot.edit_message_text(
            chat_id=message.chat.id,
            message_id=original_msg_id, # 刷新最初那条 AI 生成的消息
            text=f"🤖 **预览已更新！**\n\n{new_text}\n\n当前状态：⏳ 草稿",
            parse_mode="Markdown",
            reply_markup=gen_draft_markup(item_id) # 重新附带操作按钮
        )
        
        bot.reply_to(
            message, 
            f"📍 位置已更新：{readable_address}\n预览已同步，请在上方的预览消息中确认发布。", 
            reply_markup=types.ReplyKeyboardRemove()
        )
    except Exception as e:
        print(f"位置刷新失败: {e}")
        bot.reply_to(message, "❌ 位置更新失败，请稍后再试。")

# 在 callback_inline 处理器中增加对 "sold" 的处理
# (在你的 callback_inline 函数里加入以下分支)
# elif action == "sold":
#     supabase.table("items").update({"status": "sold"}).eq("id", item_id).execute()
#     # 顺便加点信用分
#     supabase.rpc('increment_trust', {'user_id': call.from_user.id, 'amount': 1}).execute()
#     bot.edit_message_text(f"🎉 恭喜成交！商品已标记为“已售”，信用分 +1", call.message.chat.id, call.message.message_id)

# 0.4.3.3 处理用用户签到
@bot.message_handler(commands=['sign'])
def handle_sign_in(message):
    user_id = message.from_user.id
    today = date.today().isoformat() # 获取今天的日期字符串，如 "2026-02-04"

    # 1. 获取用户信息
    profile = get_or_create_profile(message.from_user)
    last_date = profile.get('last_sign_date')

    # 2. 判断逻辑
    if last_date == today:
        bot.reply_to(message, f"👋 宝子，你今天已经领过能量啦！\n明天再来吧～ 保持好心情！✨")
    else:
        # 3. 更新积分和日期
        new_credits = profile['credits'] + 5
        try:
            supabase.table("profiles").update({
                "credits": new_credits,
                "last_sign_date": today
            }).eq("telegram_id", user_id).execute()
            
            bot.reply_to(message, f"🎉 签到成功！\n获得：+5 ⚡\n当前余额：{new_credits} ⚡\n明天也要记得来哦！")
        except Exception as e:
            print(f"签到失败: {e}")
            bot.reply_to(message, "😵 签到系统开小差了，请稍后再试。")

# 0.4.3.4 处理用户智能订阅
@bot.message_handler(commands=['sub'])
def handle_subscribe(message):
    # 格式：/sub 电脑
    keyword = message.text.replace('/sub', '').strip()
    
    if not keyword:
        # 如果只输入了 /sub，显示当前订阅列表
        subs = supabase.table("subscriptions").select("keyword").eq("telegram_id", message.from_user.id).execute()
        if not subs.data:
            bot.reply_to(message, "🔍 你还没有订阅任何关键词。发送 `/sub 关键词` 即可开启提醒。", parse_mode="Markdown")
        else:
            list_text = "\n".join([f"• {s['keyword']}" for s in subs.data])
            bot.reply_to(message, f"📋 **当前订阅词：**\n{list_text}\n\n发送 `/unsub 关键词` 可取消。")
        return

    # 存入数据库
    supabase.table("subscriptions").insert({
        "telegram_id": message.from_user.id,
        "keyword": keyword
    }).execute()
    
    bot.reply_to(message, f"✅ 订阅成功！一旦有邻居发布【{keyword}】，我会立刻通知你。")

# 0.4.3.4.1 处理用户取消订阅 : 后续测试
@bot.message_handler(commands=['unsub'])
def handle_unsubscribe(message):
    # 1. 提取指令后的关键词
    keyword = message.text.replace('/unsub', '').strip()
    
    # 2. 如果用户只输入了 /unsub，没有带关键词
    if not keyword:
        # 查询该用户所有的订阅
        subs = supabase.table("subscriptions").select("keyword").eq("telegram_id", message.from_user.id).execute()
        
        if not subs.data:
            bot.reply_to(message, "📭 你目前没有任何活跃的订阅。")
        else:
            # 列出所有关键词，并引导用户如何取消
            list_text = "\n".join([f"• `{s['keyword']}`" for s in subs.data])
            response = (
                f"📋 **您的当前订阅列表：**\n\n{list_text}\n\n"
                f"💡 **如何取消？**\n"
                f"请发送 `/unsub 关键词`，例如：`/unsub 电脑`"
            )
            bot.reply_to(message, response, parse_mode="Markdown")
        return

    # 3. 执行删除逻辑
    try:
        # 尝试从数据库删除匹配的订阅记录
        res = supabase.table("subscriptions").delete().eq("telegram_id", message.from_user.id).eq("keyword", keyword).execute()
        
        # 判断是否真的删除了数据（res.data 包含被删除的行）
        if res.data and len(res.data) > 0:
            bot.reply_to(message, f"✅ 已成功取消对【{keyword}】的捡漏订阅。")
        else:
            bot.reply_to(message, f"❓ 未找到关于【{keyword}】的订阅，请检查拼写是否一致。")
            
    except Exception as e:
        print(f"取消订阅操作失败: {e}")
        bot.reply_to(message, "😵 系统暂时无法处理您的请求，请稍后再试。")

# 0.4.3.5 处理用户充值指令
@bot.message_handler(commands=['recharge'])
def recharge_command(message):
    markup = types.InlineKeyboardMarkup(row_width=1)
    # 定义不同的充值/订阅选项
    markup.add(
        types.InlineKeyboardButton("🔋 100 能量包 ($1)", callback_data="recharge_10_credits"),
        types.InlineKeyboardButton("💎 月度会员 ($9.9)", callback_data="recharge_50_monthly"),
        types.InlineKeyboardButton("🔥 年度会员 ($80)", callback_data="recharge_99_yearly")
    )
    
    pay_info = (
        "⚡ **华邻易市 · 充值中心**\n\n"
        "**[套餐说明]**\n"
        "• 能量包：即买即用，适合偶尔出货。\n"
        "• 会员制：有效期内发布免能量，且在商城享有【优质卖家】标识。\n\n"
        "**[支付方式]**\n"
        "Paypal: `smallsky163@gmail.com` (请备注 ID: `{}`)\n\n"
        "**[确认充值]如充值失败请联系管理员：@likkcho996 **\n"
        "转账后请**直接发送支付截图**，我们将尽快为您处理。".format(message.from_user.id)
    )
    bot.send_message(message.chat.id, pay_info, reply_markup=markup, parse_mode="Markdown")

# 专门监听查看特定商品的指令
@bot.message_handler(regexp=r'/view_(\d+)')
def handle_view_item(message):
    item_id = message.text.split('_')[1]
    # 调用渲染引擎显示该商品的预览及按钮
    text = get_latest_preview_text(item_id)
    # 重用之前的草稿/管理按钮
    markup = gen_draft_markup(item_id) 
    bot.send_message(message.chat.id, text, reply_markup=markup, parse_mode="Markdown")

def process_photo_task(message):
    # 1. 定义一个卖货专家的系统指令
    MARKETING_PROMPT = """
    你是一个精通小红书流量密码的海外二手交易专家。
    请根据图片分析商品，并输出以下结构的内容：
    1. 【文案部分】：
    - 包含爆款标题（带 Emoji）。
    - 宝贝描述（成色、感受、转手原因）。
    - 诚心价格、标签。
    - 语言要亲切（如：宝子、绝绝子）。

    2. 【数据部分】：
    请在文案最后一行，严格按照以下格式输出（不要有任何额外字符）：
    DATA:商品名|价格数字

    例如：
    DATA:iPhoneX|180
    """
    try:
        print("收到照片，正在分析...")

        # --- 🌟 新增：判断是否为充值截图 ---
        caption = message.caption or ""
        if "充值" in caption or "支付" in caption:
            # 尝试从附言中提取金额，或者让用户先点按钮记录状态（进阶做法）
            # 这里我们简化处理：管理员手动决定或根据用户之前的选择
            user_id = message.from_user.id
            admin_markup = types.InlineKeyboardMarkup(row_width=1) # 设置为 1 方便点击
            
            # 构造包含金额和类型的 callback_data: refill_ok_用户ID_金额_套餐类型
            admin_markup.add(
                types.InlineKeyboardButton("✅ 准予：1刀 (100能量)", callback_data=f"refill_ok_{user_id}_10_credits"),
                types.InlineKeyboardButton("✅ 准予：9.9刀 (月度会员)", callback_data=f"refill_ok_{user_id}_50_monthly"),
                types.InlineKeyboardButton("✅ 准予：80刀 (年度会员)", callback_data=f"refill_ok_{user_id}_99_yearly"),
                types.InlineKeyboardButton("❌ 拒绝申请", callback_data=f"refill_no_{user_id}")
            )
            
            bot.forward_message(ADMIN_ID, message.chat.id, message.message_id)
            bot.send_message(ADMIN_ID, f"🔔 **收到充值申请**\n来自用户：`{user_id}`\n用户名：@{message.from_user.username}", 
                            reply_markup=admin_markup, parse_mode="Markdown")
            bot.reply_to(message, "📩 支付凭证已提交，请耐心等待管理员审核。")
            return
        # --- 识图流程优化 ---
        print(f"收到照片分析请求，附言: {caption}")
        
        # 获取最高画质的照片
        photo_file_id = message.photo[-1].file_id
        
        print("正在压缩并上传图片...")
        # 2. 【执行前置】先压缩并上传，同时拿回压缩后的二进制数据供 AI 使用
        bot.send_chat_action(message.chat.id, 'upload_photo')
        image_url, compressed_data = upload_to_supabase(photo_file_id)
        
        if not image_url or not compressed_data:
            bot.reply_to(message, "❌ 图片处理失败，请稍后再试。")
            return
        
        # 构造符合 Gemini SDK 要求的图片部分
        #image_parts = [
        #    {
        #        "mime_type": "image/jpeg",
        #        "data": downloaded_file
        #    }
        #]
        
        # 组合指令
        prompt_parts = [
            MARKETING_PROMPT, 
            {"mime_type": "image/jpeg", "data": compressed_data},
            f"用户补充信息（极其重要，若与图片冲突以此为准）: {caption}" 
        ]           
        # --- 新增：积分检查 ---
        profile = get_or_create_profile(message.from_user)
        # 1. 检查会员是否有效
        is_vip = False
        if profile.get('subscription_expiry'):
            from datetime import datetime, timezone
            # 解析数据库存的时间字符串
            try:
                expiry_date = datetime.fromisoformat(profile['subscription_expiry'].replace('Z', '+00:00'))
                if expiry_date > datetime.now(timezone.utc):
                    is_vip = True
            except Exception as e:
                print(f"日期解析出错: {e}")

        # 2. 判定逻辑
        if is_vip:
            print(f"用户 {message.from_user.id} 是会员，免扣费识图。")
            bot.send_chat_action(message.chat.id, 'typing') # 给个反馈提示
        elif profile['credits'] < 10:
            bot.reply_to(message, f"❌ 能量不足！\n当前余额：{profile['credits']} ⚡\n识图需消耗 10 ⚡，请回复“充值”发送截图或等待明日签到。")
            return 
        
        print(f"用户 {message.from_user.id} 余额充足，准备识图...")

        # 获取 AI 生成的高质量文案
        response = model.generate_content(prompt_parts)
        full_text = response.text

        # 我们使用 splitlines 处理，过滤掉包含特定关键词的行
        lines = full_text.splitlines()
        clean_lines = [
            line for line in lines 
            if "【文案部分】" not in line and "【数据部分】" not in line
        ]

        # 重新组合成纯净的文案
        display_text1 = "\n".join(clean_lines).strip()

        # --- 核心提取逻辑 ---
        item_title = "未知商品" # 默认值
        price_val = "0"        # 默认值
        
        # --- 识图成功后：正式扣除 10 积分 ---
        # --- 识图成功后：扣费判定 ---
        if not is_vip:
            new_balance = profile['credits'] - 10
            supabase.table("profiles").update({"credits": new_balance}).eq("telegram_id", message.from_user.id).execute()
            print(f"非会员积分已扣除，剩余：{new_balance}")
        else:
            print("会员用户，跳过扣费步骤。")
        # ---------------------

        try:
            # 1. 提取 AI 数据（逻辑同前）
            for line in display_text1.split('\n'):
                if line.startswith("DATA:"):
                    data_part = line.replace("DATA:", "").strip()
                    item_title, price_val = data_part.split('|')
                    break
            display_text = display_text1.split("DATA:")[0].strip()

            # 2. 插入数据库，状态设为 draft
            res = supabase.table("items").insert({
                "name": item_title,
                "price": float(price_val),
                "description": display_text,
                "username": message.from_user.username,
                "status": "draft", # 关键：初始为草稿
                "telegram_id": message.from_user.id,
                "image_url": image_url # 🌟 存入图片直连
            }).execute()
            
            item_id = res.data[0]['id'] # 获取这条记录的 ID

            # 3. 创建 V1.2 交互按钮
            markup = types.InlineKeyboardMarkup(row_width=2)
            btn_confirm = types.InlineKeyboardButton("✅ 确认发布", callback_data=f"conf_{item_id}")
            btn_edit_price = types.InlineKeyboardButton("💰 改价格", callback_data=f"editp_{item_id}")
            btn_edit_desc = types.InlineKeyboardButton("📝 改描述", callback_data=f"editd_{item_id}") # 🌟 新增
            btn_location = types.InlineKeyboardButton("📍 加位置", callback_data=f"loc_{item_id}")
            btn_cancel = types.InlineKeyboardButton("❌ 撤回", callback_data=f"del_{item_id}")
            # 建议排列方式：确认按钮独占一行，其他两两一排
            markup.add(btn_confirm)
            markup.add(btn_edit_price, btn_edit_desc)
            markup.add(btn_location, btn_cancel)

            # 1. 净化文案
            raw_text = display_text # Gemini 生成的原始文案
            safe_text = escape_markdown(raw_text)
            
            bot.reply_to(message, f"🤖 **AI 预览生成成功！**\n\n{safe_text}\n\n当前状态：⏳ 草稿（未上架）", reply_markup=markup, parse_mode="Markdown")
            
        except Exception as e:
            print(f"解析数据失败: {e}")
            display_text = display_text1.split("DATA:")[0].strip()

        
        print("照片分析完成并回复。")
    except Exception as e:
        print(f"Error: {e}")
        bot.reply_to(message, "宝子，AI 大脑卡壳了，请稍后再试～")

# 0.4.3.6 处理用户智能分析
@bot.message_handler(func=lambda m: True, content_types=['text', 'photo'])
def handle_message(message):
    if message.content_type == 'photo':
        # 启动后台线程处理图片，实现“秒派发”
        threading.Thread(target=process_photo_task, args=(message,)).start()
        print(f"🚀 图片任务已派发 (Message ID: {message.message_id})")


# print("🚀 华邻助手正式启动 (Gemini 2.5 Flash)...")
# bot.infinity_polling()

# --- 在启动 Bot 前开启 Flask 线程 ---
# 修改启动部分
if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    
    print("Bot 正在尝试连接 Telegram 服务器...")
    
    # 使用更加鲁棒的启动方式
    # timeout 设置长一点，并且开启 non_stop 重试
    bot.infinity_polling(timeout=60, long_polling_timeout=60)
