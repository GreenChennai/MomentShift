"""图形用户界面包，基于 PyQt6 + PyQt6-Fluent-Widgets 构建。

职责边界：
- 做：容纳主窗口、各功能页与共享控件。
- 不做：不实现业务逻辑，全部下沉到 core（界面只负责取值、展示与转发）。

依赖：core、i18n；被依赖：__main__、quick_runner。
"""
