# 使用轻量级 Python 镜像
FROM python:3.9-slim

# 设置工作目录
WORKDIR /app

# 复制当前目录下的所有文件到镜像中
COPY . .

# 安装依赖
RUN pip install --no-cache-dir -r requirements.txt
# 暴露 HF 要求的端口
EXPOSE 7860
# 启动命令：运行你的主脚本
CMD ["python", "hualin0.3.py"]

