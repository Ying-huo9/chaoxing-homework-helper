# 超星作业抓取助手

一个面向 Windows 的超星学习通作业整理工具，支持从本地 MHTML 网页快照解析题目，也支持通过受控浏览器读取课程中的作业与章节练习，并导出为 Word、JSON 和易读文本。

## 功能

- 本地解析 `.mht` / `.mhtml` 作业页面
- 在线登录后读取课程、作业和章节练习
- 自动合并重复入口和重复题目
- 将图片下载并嵌入 Word（可选）
- 导出 `.docx`、`.json` 和 `.txt`
- 支持系统 Chrome、Microsoft Edge 和便携 Chromium
- 使用 Windows 当前账户加密用户选择保存的密码

## 环境要求

- Windows 10 或 Windows 11
- Python 3.10 或更高版本
- Chrome、Microsoft Edge，或放在 `browser` 目录中的便携 Chromium

## 从源码运行

```powershell
git clone https://github.com/Ying-huo9/chaoxing-homework-helper.git
cd chaoxing-homework-helper
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python main.py
```

第一次在线使用时，程序会打开浏览器供你登录。遇到验证码时请在浏览器中手动完成验证。

## 使用方式

### 本地模式

1. 选择包含 `.mht` 或 `.mhtml` 文件的目录。
2. 按需填写课程、班级和姓名，并选择是否下载图片。
3. 点击“开始处理”。
4. 在所选目录的 `产出文件` 子目录中查看结果。

### 在线模式

1. 选择浏览器以及是否始终显示浏览器窗口。
2. 输入账号和密码，点击“登录并读取课程”。
3. 选择“作业”或“章节练习”，进入一门课程。
4. 读取列表后开始抓取，并在所选保存目录中查看结果。

更完整的操作说明见 [README_USER.md](README_USER.md)。

## 测试

```powershell
python -m unittest discover -s . -p "test_*.py" -v
```

## 打包

安装依赖后可使用 PyInstaller 配置进行构建：

```powershell
pyinstaller --clean --noconfirm "超星作业抓取助手.spec"
```

## 隐私与安全

- 仓库不会跟踪 `settings.json`、浏览器配置、日志、网页快照或课程导出文件。
- 选择“记住账号和密码”时，密码由 Windows 当前用户凭据加密，不以明文保存。
- 请不要提交自己的学习通账号、Cookie、浏览器用户数据或课程资料。
- 如果旧版本脚本中曾写入明文密码，请立即修改该密码。

## 使用边界

本项目仅用于整理你有权访问的学习资料。请遵守所在学校、超星学习通的相关规则以及适用法律，不要用于绕过访问控制、批量滥用服务或传播未获授权的课程内容。

## 许可证

[MIT License](LICENSE)
