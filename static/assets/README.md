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

## 背景图（三选一，切换只需改 theme.css 两行）

`theme.css` 里 `--page-bg-image` 与 `--page-scrim-color` 要**成对**改：

| 方案 | 图 | 纱 | 大小 | 说明 |
|---|---|---|---|---|
| **A（当前默认）** | `page-bg-blue.webp` | `.45` | 63 KB | 使用者给的两张之一 |
| B | `page-bg.webp` | `.30` | 37 KB | 本体的原背景 |
| C | `page-bg-forest.webp` | `.65` | 748 KB | 使用者给的两张之一 |

### 实测：不同底图 × 纱浓度（1280×800，数值 = 低于 WCAG 阈值的元素数 / 压在底图上文字的最低对比度）

| 底图 | `napcat.html` @.30 | @.45 | @.55 | @.65 | `index.html` @.30 | @.45 | @.65 |
|---|---|---|---|---|---|---|---|
| 纯色（无图） | 16 / 3.70 | — | — | — | 3 / 3.06 | — | — |
| B 本体 37 KB | 18 / 3.49 | 16 / 3.55 | 16 / 3.58 | 16 / 3.62 | 4 / 3.04 | 4 / 3.05 | 3 / 3.06 |
| **A blue 63 KB** | 21 / 3.34 | **17 / 3.42** | 16 / 3.48 | 16 / 3.54 | 3 / 2.88 | **3 / 2.92** | 3 / 2.98 |
| C forest 748 KB | 23 / 3.05 | 23 / 3.19 | 20 / 3.29 | 16 / 3.39 | **14 / 1.90** | 14 / 2.41 | 5 / 2.87 |

四页合计不达标：**纯色底 37 / A+纱.45 = 37 / B+纱.30 = 39 / C+纱.30 = 51**。

* **A 配 .45 在可读性上和纯色底打平**（37 对 37），所以选它当默认。
* **C 明显更贵**：`index.html` 的文字直接压在底图上、没有卡片挡，细节多的插画一上来
  就从 3 处涨到 14 处，要压到纱 .65 才回到 5 处 —— 但那时画面已经发白。
  若确实想用 C，更好的做法是把 `--card-bg` 改回 `var(--surface-strong)`（卡片更实）。
* 纱能买到的东西**有限**：剩下的低对比度主要来自「同色字压同色淡底」的徽标/按钮，
  与底图无关，加纱救不了。

### 使用者给的两张（来源：桌面）

| 文件 | 桌面原件 | 源尺寸 | 源大小 |
|---|---|---|---|
| `page-bg-blue.webp` | `b33b6c1e7a8e3b7824c9acfd57e628cb.jpg` | 1672×940 | 121 KB |
| `page-bg-forest.webp` | `forest-nap-4k-sharp.jpg` | 3840×2160 | 4657 KB |

压缩（保真度按 **8×8 区块**算最差块，不看全局 PSNR）：

| 产物 | 设定 | 大小 | 最差块 |
|---|---|---|---|
| `page-bg-blue.webp` | 原尺寸 q=88 | 63 KB | 39.52 dB |
| `page-bg-forest.webp` | 缩到 2560×1440 q=90 | 748 KB | 35.36 dB |

```python
from PIL import Image
src = Image.open(SRC)
src.resize((2560, 1440), Image.LANCZOS).convert("RGB").save(DEST, "WEBP", quality=90, method=6)
```

森林那张是细节丰富的插画（草/叶/花），**压不动**：1920 长边 q=86 = 423 KB 时最差块只有
32.19 dB（局部已可见损坏）；要到 2560 q=90 = 748 KB 才干净。因为它从**本地插件服务器**
加载而不是走网络，所以这里把体积权重放低、保真度放高。

SHA-256：

```
page-bg-blue.webp    C3D0C56D74D07E637E434AC10024027218FBC5AC25D087660CC1F4E67DB9C7FA
page-bg-forest.webp  DAAAB2899680C5106969CECD092C073B79E29DCEFB7C38E56EE675A90619B5FB
page-bg.webp         3940BF037D93CFE98A4F5A0B2E91D8C652907A3AFC5EB5F1DC4F7A185CD396E7
```

## 本体的原背景（方案 B）

| 项 | 值 |
|---|---|
| 来源 | `static/icons/model_manager_background.png`（本体 `model_manager.css` / `live2d_parameter_editor.css` 里就是 `html,body` 的整页背景） |
| 源 | 1920×1080 RGBA PNG，395 KB |
| 产物 | `page-bg.webp` 1920×1080 RGB，**37 KB**（WebP q=88，省 91%） |
| 保真度 | 8×8 区块**最差块 44.67 dB** |

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
