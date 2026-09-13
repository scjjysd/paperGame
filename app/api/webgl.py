"""Unity WebGL 静态资源：复用 StaticFiles 的路径校验、条件请求和文件传输。"""
from pathlib import Path

from fastapi.staticfiles import StaticFiles


class WebGLStaticFiles(StaticFiles):
    """为 Unity 预压缩产物补齐浏览器解压与 WASM 流式编译所需的响应头。"""

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
