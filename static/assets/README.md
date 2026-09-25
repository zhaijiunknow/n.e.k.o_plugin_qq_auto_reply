# static/assets —— 从本体复制的品牌素材与背景图

这里的文件**不是**本插件原创，是从本体（N.E.K.O 主程序）里取过来的，为的是让插件的
界面和本体保持一致。取过来而不是外链，是因为插件要求**离线可用**（见 `index.html`
里的注释：不引外部资源）。

## 品牌素材（原样复制，逐字节校验）

| 文件 | 来源 | 本体里的用处 |
|---|---|---|
| `neko-logo.png` | `frontend/plugin-manager/src/assets/neko-logo.png` | `layout/Sidebar.vue` 的侧栏品牌（28×28、圆角 8、配 17px/800 文字） |
| `paw.png` | `frontend/plugin-manager/src/assets/paw.png` | `layout/AppLayout.vue` 标题栏左端的猫爪（20×16） |

复制时的 SHA-256（用于确认没有在途中被改过）：

```
neko-logo.png  B1868BC5557AC2CB5F532F50B40DD85BED42FE165ECFAFEC588AAD6E84B893A1
paw.png        96039C96CED97179A2E1E366388117E78255AEDE4EB3B1026D653BA5CEF57CDD
```

## 背景图（重新编码过，不是原样复制）

| 项 | 值 |
|---|---|
| 来源 | `static/icons/model_manager_background.png`（本体 `model_manager.css` / `live2d_parameter_editor.css` 里就是 `html,body` 的整页背景） |
| 源 | 1920×1080 RGBA PNG，395 KB |
| 产物 | `page-bg.webp` 1920×1080 RGB，**37 KB**（WebP q=88，省 91%） |
| 保真度 | 按 **8×8 区块**分别算 PSNR，**最差的一块 44.67 dB**（不是只看全局平均 —— 大片平滑渐变会把平均分拉高，掩盖局部损坏） |
| SHA-256 | `3940BF037D93CFE98A4F5A0B2E91D8C652907A3AFC5EB5F1DC4F7A185CD396E7` |

重新生成：

```python
from PIL import Image
Image.open(SRC).save(DEST, "WEBP", quality=88, method=6)
```

### 为什么是这张

三张 1920×1080 的候选，按 3×3 区域量化过（不看图，只看数字）：

| 候选 | 结构 | 边缘密度 | 大小 |
|---|---|---|---|
| `subscriptions-content_bg.png` | 3×3 网格几乎全等（角差 3）→ **就是一块纯色** | 0.246 | 71 KB |
| `steam_shop_bg.png` | 上深下浅的**纯渐变**，每行左右完全一致 | 0.193 | 84 KB |
| `model_manager_background.png` | 同样的渐变**+ 上面有画** | 0.918 | 395 KB |

只有第三张真的有画面，而且它本来就是本体的整页背景。

### 底图上那层「纱」

`theme.css` 的 `--page-scrim-color`（当前 0.30）是压在底图上的一层白纱，做法来自本体
（`character_card_manager.css` 用 `linear-gradient(...), url(...)`）。

**这个值是审美取值，不是为对比度。** 实测把它从 0 加到 0.75，压在底图上的文字对比度
只从 3.39 爬到 3.66，而底图影响度（合成底色与纯色 `--bg` 的最大通道差）从 55 掉到 8，
等于把图盖死。原因是剩下的低对比度主要来自「同色字压同色淡底」的徽标/按钮，跟底图
无关，加纱救不了。

底图带来的**真实**代价（实测，1280×800）：

| 元素 | 加底图前 | 加底图后 |
|---|---|---|
| `index.html` 副标题 `p.sub`（直接压在底图上） | ~4.8 | **3.95** |
| `status.html` 错误框 | 3.75 | 2.79 |
| 侧栏导航文字 | 3.70–3.78 | 3.39–3.48 |
| ghost 按钮 | 3.72 | 3.55–3.63 |

其中只有 `p.sub` 是**原本达标、被底图压到不达标**的；其余是原本就不达标（本体色板
自带），被底图再压低 0.1–1.0。

## 本体改了这些图怎么办

品牌图重新复制即可：

```powershell
Copy-Item frontend\plugin-manager\src\assets\neko-logo.png plugin\plugins\qq_auto_reply\static\assets\ -Force
Copy-Item frontend\plugin-manager\src\assets\paw.png       plugin\plugins\qq_auto_reply\static\assets\ -Force
```

换底图：改 `theme.css` 的 `--page-bg-image`，并把 `?v=` 加一。换完记得把上面的哈希更新掉。

注意三处**有意**和本体不同：

* 本体的标题栏是蓝色渐变，所以那只猫爪加了 `filter: brightness(0) invert(1)` 变成纯白。
  插件的顶栏是浅色的，**不加这个滤镜**，保留原色。
* 本体侧栏品牌文字是 `N.E.K.O`；本插件显示自己的名字（`QQ 控制台`），走 i18n 键
  `ui.shared.sidebar.logo`。
* 底图重新编码成了 WebP（q=88）而不是原样复制 PNG，纯粹为了体积。
