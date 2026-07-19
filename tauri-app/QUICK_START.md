# API Node Manager - 快速启动

## 一键打开

双击：

```text
启动应用.bat
```

优先打开：

```text
dist\API-Node-Manager.exe
```

## 重新生成 exe

双击：

```text
打包EXE.bat
```

生成：

```text
dist\API-Node-Manager.exe
```

这个脚本默认执行：

```powershell
npm run build -- --no-bundle
```

也就是只生成可运行 exe，不下载 NSIS 安装器。

## 从源码启动

双击：

```text
启动源码版.bat
```

源码有更新时会自动重新构建 release exe，然后打开应用。

## 调试模式

需要看日志或热重载时才用：

```text
启动开发模式.bat
```

## 输出位置

```text
dist\API-Node-Manager.exe
src-tauri\target\release\api-node-manager.exe
```

## 常见问题

如果提示缺少 `npm`，安装 Node.js。

如果提示缺少 `cargo`，安装 Rust。

如果完整安装包构建时下载 NSIS 超时，不影响 `dist\API-Node-Manager.exe` 使用。
