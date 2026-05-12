# Mock Tool

一个单文件的 Python Mock 服务，带前端管理页面，支持路径参数化与响应体占位符。

## 运行

```bash
pip install -r requirements.txt
python mock_tool.py
```

打开 http://127.0.0.1:5000/ 录入接口，调用 `http://127.0.0.1:5000/mock/<你的路径>` 即可命中。

## 参数化能力

- 路径参数：`/users/{id}` → 请求 `/mock/users/123`
- 响应体占位符（在响应正文任何位置插入）：
  - `{{path.id}}`：路径参数
  - `{{query.name}}`：URL query 参数
  - `{{body.xxx}}`：JSON / form 请求体字段
  - `{{header.Authorization}}`：请求头
  - `{{random.int}}` / `{{random.uuid}}` / `{{random.float}}`
  - `{{now}}`：当前时间（ISO）

响应体按字符串做占位替换，可以是任意 Content-Type，不限 JSON。

## 持久化

所有接口保存在同目录下 `mocks.json`。

