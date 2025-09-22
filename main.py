import asyncio
import json
from typing import List, Dict, Any
import httpx
from fastapi import FastAPI, Request, HTTPException
from starlette.responses import StreamingResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from utils import get_config
from pydantic import BaseModel
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
import logging
import time
from urllib.parse import unquote
import os

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("log.txt", encoding="utf-8"),
        logging.StreamHandler() # Also log to console
    ]
)
# 设置 httpx 日志记录器的级别为 WARNING
logging.getLogger("httpx").setLevel(logging.WARNING)

site_config = get_config()

class BaseUrlItem(BaseModel):
    """单个基础URL的数据结构"""
    name: str
    base_url: str

class SiteConfigModel(BaseModel):
    """完整的站点配置数据结构"""
    site_name: str
    pc_background_image_url: str
    phone_background_image_url: str
    timeout: int
    base_urls: List[BaseUrlItem]

# 初始化FastAPI应用
app = FastAPI()
@app.middleware("http")
async def log_requests(request: Request, call_next):
    """
    Middleware to log incoming requests.
    """
    start_time = time.time()
    excluded_paths = ["/log_file"]

    # Check if the request path is in the excluded list
    if request.url.path in excluded_paths:
        # If it is, just pass the request to the next handler and return
        response = await call_next(request)
        return response
    
    # 优先从 Cloudflare 专用的头中获取真实IP
    real_ip = request.headers.get("CF-Connecting-IP")

    # 如果没有，尝试从通用的 X-Forwarded-For 头获取
    # 注意：这个头可以被伪造，如果你在Cloudflare后还有其他代理，需要更复杂的处理
    if not real_ip:
        real_ip = request.headers.get("X-Forwarded-For")
        if real_ip:
            # X-Forwarded-For 可能会包含多个IP，取第一个
            real_ip = real_ip.split(',')[0].strip()

    # 如果以上都没有，就回退到原始连接IP
    if not real_ip:
        real_ip = request.client.host

    user_agent = request.headers.get('user-agent', 'unknown')

    decoded_url = unquote(str(request.url))

    # Log incoming request details
    logging.info(f"Request: {request.method} {decoded_url} | IP: {real_ip} | User-Agent: {user_agent}")
    
    response = await call_next(request)
    process_time = time.time() - start_time
    
    # Log outgoing response details
    # logging.info(f"Response: Status Code {response.status_code} | Process Time: {process_time:.4f}s")
    
    return response

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 生产请改为具体域名
    allow_methods=["*"],
    allow_headers=["*"],
)
# 挂载 static 文件夹，使其内容可通过 /static URL 访问
app.mount("/static", StaticFiles(directory="static"), name="static")
# 设置模板目录
templates = Jinja2Templates(directory="templates")

def parse_cms_data(source_name: str, cms_list: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    将从苹果CMS接口获取的原始列表数据解析为我们需要的格式。

    Args:
        source_name: 数据源名称 (例如: "XX资源").
        cms_list: 从CMS接口返回的 "list" 字段的内容.

    Returns:
        一个符合前端要求格式的字典.
    """
    results = []
    for item in cms_list:
        # vod_play_url 的格式通常是 '播放源1$$$播放源2'
        # 我们只取第一个播放源
        play_urls_str = item.get("vod_play_url", "").split("$$$")[0]
        
        videos = []
        # 视频列表以 '#' 分割
        episodes = play_urls_str.split('#')
        for episode in episodes:
            # 每一集是 '剧集名$播放链接'
            parts = episode.split('$')
            if len(parts) == 2:
                video_name, video_url = parts
                videos.append({"name": video_name, "video_url": video_url})

        # 只有当成功解析出视频时才添加该条目
        if videos:
            results.append({
                "name": item.get("vod_name", "未知名称"),
                "vod_pic": item.get("vod_pic", ""),
                "videos": videos,
                "vod_id": item.get("vod_id",""),
                "vod_douban_id": item.get("vod_douban_id","")
            })
            
    return {
        "name": source_name,
        "result": results
    }


async def fetch_and_process(client: httpx.AsyncClient, source: Dict[str, str], keyword: str) -> Dict[str, Any] | None:
    """
    异步获取单个API源的数据并进行处理。

    Args:
        client: httpx.AsyncClient 实例.
        source: 包含 "name" 和 "base_url" 的字典.
        keyword: 搜索关键词.

    Returns:
        处理好的数据字典，如果出错则返回 None.
    """
    url = f"{source['base_url']}?ac=detail&wd={keyword}"
    name = source['name']
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    }
    try:
        # print(f"开始搜索: {name} -> {url}")
        # 设置5秒超时
        response = await client.get(
            url, 
            timeout=site_config["timeout"], 
            headers=headers, 
            follow_redirects=True  # 允许客户端自动处理302重定向
        )
        response.raise_for_status()  # 如果HTTP状态码是 4xx 或 5xx，则抛出异常
        
        data = response.json()
        
        if data.get("code") == 1 and data.get("list"):
            # print(f"成功: {name}")
            return parse_cms_data(name, data["list"])
        else:
            # print(f"数据为空或格式错误: {name}, message: {data.get('msg')}")
            return None
            
    except httpx.TimeoutException:
        # print(f"超时: {name}")
        return None
    except Exception as e:
        # print(f"请求或处理时发生错误: {name}, 错误: {e}")
        return None


async def search_event_generator(keyword: str, sources: List[Dict[str, str]]):
    """
    用于SSE的异步生成器函数。

    Args:
        keyword: 搜索关键词.
        sources: API源列表.
    """
    # 使用单个httpx.AsyncClient实例来复用连接
    async with httpx.AsyncClient() as client:
        # 为每个API源创建一个异步任务
        tasks = [
            asyncio.create_task(fetch_and_process(client, source, keyword))
            for source in sources
        ]

        # 使用 asyncio.as_completed，哪个任务先完成就先处理哪个
        for future in asyncio.as_completed(tasks):
            result = await future
            # 如果结果有效（不为None），则通过SSE发送
            if result and result.get("result"): # 确保result不为空
                # SSE消息格式: "data: <json_string>\n\n"
                yield f"data: {json.dumps(result, ensure_ascii=False)}\n\n"


@app.get("/search")
async def search(keyword: str):
    """
    并行搜索接口，使用SSE流式返回结果。
    """
    if not keyword:
        return {"error": "keyword is required"}
        
    return StreamingResponse(
        search_event_generator(keyword, site_config["base_urls"]),
        media_type="text/event-stream"
    )

@app.get("/config")
async def get_site_config():
    return site_config


@app.post("/config")
async def update_config(config_data: SiteConfigModel):
    """
    接收JSON配置，先写入本地文件，然后更新到全局变量 site_config
    """
    global site_config  # 声明我们要修改的是全局变量 site_config

    try:
        # 1. 将提交的Pydantic模型转换为JSON字符串，并写入本地文件
        #    使用 model_dump_json 可以方便地生成格式化的JSON字符串
        #    indent=4 是为了让JSON文件内容更易读
        config_json_string = config_data.model_dump_json(indent=4)
        file_path = "config.json"
        
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(config_json_string)

        # 2. 将Pydantic模型转换为字典，并赋值给全局变量 site_config
        #    使用 model_dump() 方法
        site_config = config_data.model_dump()

        return {
            "message": "Configuration updated and saved successfully.",
            "file_path": file_path,
            "current_config": site_config  # 返回当前的配置以供确认
        }

    except IOError as e:
        # 如果文件写入失败，抛出HTTP异常
        raise HTTPException(
            status_code=500, 
            detail=f"Failed to write config file: {e}"
        )
    except Exception as e:
        # 捕获其他可能的异常
        raise HTTPException(
            status_code=500, 
            detail=f"An unexpected error occurred: {e}"
        )


@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    """
    这个接口会渲染并返回一个带有动态标题的 HTML 页面。
    """
    # 定义要传递给模板的变量
    template_variables = {
        "request": request,
        "site_config": site_config
    }
    
    # 返回模板响应
    return templates.TemplateResponse("index.html", template_variables)

def file_iterator(file_path: str, chunk_size: int = 4096):
    """
    一个同步生成器，用于分块读取文件。
    """
    with open(file_path, "r", encoding="utf-8") as f:
        while chunk := f.read(chunk_size):
            yield chunk

@app.get("/log_file")
async def get_log_file():
    """
    使用同步生成器流式传输日志文件。
    """
    log_file_path = "log.txt"

    if not os.path.exists(log_file_path):
        raise HTTPException(status_code=404, detail="Log file not found.")

    # 将同步生成器传递给 StreamingResponse
    # FastAPI 会自动在后台线程中运行它
    return StreamingResponse(
        file_iterator(log_file_path), 
        media_type="text/plain",
        headers={
            "Content-Disposition": "inline"
        }
    )