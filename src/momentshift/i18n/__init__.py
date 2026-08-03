"""国际化包，JSON 语言文件位于 locales 子目录。

职责边界：
- 做：暴露 translator 单例与 tr() 快捷函数。
- 不做：不承载任何界面逻辑。

依赖：core/logger；被依赖：全部界面模块与 quick_runner。
"""
