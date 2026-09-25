# static/assets —— 从本体复制的品牌素材

这两个文件**不是**本插件原创，是从本体（N.E.K.O 主程序）的前端插件管理器里复制过来的，
为的是让插件的界面品牌标记和本体保持一致。复制而不是外链，是因为插件要求**离线可用**
（见 `index.html` 里的注释：不引外部资源）。

| 文件 | 来源 | 本体里的用处 |
|---|---|---|
| `neko-logo.png` | `frontend/plugin-manager/src/assets/neko-logo.png` | `layout/Sidebar.vue` 的侧栏品牌（28×28、圆角 8、配 17px/800 文字） |
| `paw.png` | `frontend/plugin-manager/src/assets/paw.png` | `layout/AppLayout.vue` 标题栏左端的猫爪（20×16） |

复制时的 SHA-256（用于确认没有在途中被改过）：

```
neko-logo.png  B1868BC5557AC2CB5F532F50B40DD85BED42FE165ECFAFEC588AAD6E84B893A1
paw.png        96039C96CED97179A2E1E366388117E78255AEDE4EB3B1026D653BA5CEF57CDD
```

## 本体改了这两个图怎么办

重新复制即可，然后把上面的哈希更新掉：

```powershell
Copy-Item frontend\plugin-manager\src\assets\neko-logo.png plugin\plugins\qq_auto_reply\static\assets\ -Force
Copy-Item frontend\plugin-manager\src\assets\paw.png       plugin\plugins\qq_auto_reply\static\assets\ -Force
```

注意两处**有意**和本体不同：

* 本体的标题栏是蓝色渐变，所以那只猫爪加了 `filter: brightness(0) invert(1)` 变成纯白。
  插件的顶栏是浅色的，**不加这个滤镜**，保留原色。
* 本体侧栏品牌文字是 `N.E.K.O`；本插件显示自己的名字（`QQ 控制台`），走 i18n 键
  `ui.shared.sidebar.logo`。
