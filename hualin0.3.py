import os
import telebot
import google.generativeai as genai
from telebot.types import MenuButtonWebApp, WebAppInfo
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from supabase import create_client, Client
import threading
from flask import Flask
from telebot import TeleBot

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

@bot.message_handler(func=lambda m: True, content_types=['text', 'photo'])
def handle_message(message):
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
        if message.content_type == 'photo':
            print("收到照片，正在分析...")
            # 获取最高画质的照片
            file_info = bot.get_file(message.photo[-1].file_id)
            downloaded_file = bot.download_file(file_info.file_path)
            
            # 构造符合 Gemini SDK 要求的图片部分
            image_parts = [
                {
                    "mime_type": "image/jpeg",
                    "data": downloaded_file
                }
            ]
            
            # 组合指令
            prompt_parts = [
                MARKETING_PROMPT,
                {"mime_type": "image/jpeg", "data": downloaded_file}
            ]           
            
            # 获取 AI 生成的高质量文案
            response = model.generate_content(prompt_parts)
            full_text = response.text
            # --- 核心提取逻辑 ---
            item_title = "未知商品" # 默认值
            price_val = "0"        # 默认值
            
            try:
                # 寻找包含 DATA: 的那一行
                for line in full_text.split('\n'):
                    if line.startswith("DATA:"):
                        # 提取出 "商品名|价格"
                        data_part = line.replace("DATA:", "").strip()
                        item_title, price_val = data_part.split('|')
                        break
                
                # 将解析后的文案（去掉 DATA 行）展示给用户
                display_text = full_text.split("DATA:")[0].strip()

                try:
                    # 将商品数据存入 Supabase
                    data, count = supabase.table("items").insert({
                        "name": item_title,
                        "price": float(price_val),
                        "description": display_text
                    }).execute()
                    print("商品已成功存入数据库！")
                except Exception as e:
                    print(f"入库失败: {e}")
                
            except Exception as e:
                print(f"解析数据失败: {e}")
                display_text = full_text

            # 生成动态链接
            # 注意：使用 quote 处理中文，防止链接失效
            from urllib.parse import quote
            share_url = f"https://smallsky163.github.io/hualin-market/index.html?item={quote(item_title)}&price={price_val}"

            markup = InlineKeyboardMarkup()
            #btn = InlineKeyboardButton("✨ 预览我的精美主页", url=share_url)

            # 使用 WebAppInfo 包装你的链接，这样它就会在 Telegram 内部弹窗打开
            btn = InlineKeyboardButton(
                text="✨ 预览并发布到商城", 
                web_app=WebAppInfo(url=share_url) 
            )
            markup.add(btn)

            bot.reply_to(message, f"✨ 文案已润色：\n\n{display_text}", reply_markup=markup)
            print("照片分析完成并回复。")
    except Exception as e:
        print(f"Error: {e}")
        bot.reply_to(message, "宝子，AI 大脑卡壳了，请稍后再试～")

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