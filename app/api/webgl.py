"""Unity WebGL 静态资源：复用 StaticFiles 的路径校验、条件请求和文件传输。"""
from pathlib import Path

from fastapi.staticfiles import StaticFiles

# 已在磁盘上压缩好的产物后缀：磁盘文件是压缩流，靠 Content-Encoding 让浏览器解压。
PRE_COMPRESSED_SUFFIXES = frozenset({'.gz', '.br'})
# 需要屏蔽在预压缩产物上的请求头：分片范围对压缩流没有意义。
RANGE_HEADERS = frozenset({b'range', b'if-range'})


class WebGLStaticFiles(StaticFiles):
    """为 Unity 预压缩产物补齐浏览器解压与 WASM 流式编译所需的响应头。"""

    async def __call__(self, scope, receive, send):
        """预压缩产物忽略 Range，永远整份返回。

        `StaticFiles` 对分片请求会回 206：`Content-Range` 按**压缩后**长度计算，
        而响应又带着 `Content-Encoding: gzip`。浏览器拿到的是压缩流的片段，
        既解码不出完整内容，也无法与已下载部分拼接 —— 手机弱网下 7.7 MiB 的
        `C1WebGL.wasm.gz` 一旦触发续传，Unity 的 `getBinary` 就取不到可编译的
        wasm 并直接 `abort`（报 "both async and sync fetching of the wasm failed"）。
        预压缩产物不支持分片只会让续传退化成整份重下，比返回坏数据安全。

        分片判定发生在 `FileResponse.__call__` 内部，只认这里传下去的 scope，
        所以必须在进入 `StaticFiles` 之前改写，改不了 `get_response` 的入参。
        """
        if scope.get('type') == 'http' and Path(self.get_path(scope)).suffix in PRE_COMPRESSED_SUFFIXES:
            headers = [
                (name, value) for name, value in scope['headers']
                if name.lower() not in RANGE_HEADERS
            ]
            scope = {**scope, 'headers': headers}
        await super().__call__(scope, receive, send)

    def file_response(self, full_path, stat_result, scope, status_code=200):
        response = super().file_response(full_path, stat_result, scope, status_code)
        # Unity 默认使用固定文件名，入口和构建资源每次使用前都需要校验版本。
        response.headers['Cache-Control'] = 'no-cache'
        if response.status_code == 304:
            return response

        path = Path(full_path)
        encoding = {'.gz': 'gzip', '.br': 'br'}.get(path.suffix)
        if encoding:
            response.headers['Content-Encoding'] = encoding
            # 不再宣告支持分片：理由见 __call__。
            if 'accept-ranges' in response.headers:
                del response.headers['accept-ranges']
            path = path.with_suffix('')
        # .unityweb 由 Unity loader 解压，不添加 Content-Encoding。
        media_type = {
            '.wasm': 'application/wasm',
            '.js': 'application/javascript',
            '.data': 'application/octet-stream',
            '.json': 'application/json',
            '.unityweb': 'application/octet-stream',
        }.get(path.suffix)
        if media_type:
            response.headers['Content-Type'] = media_type
        return response
