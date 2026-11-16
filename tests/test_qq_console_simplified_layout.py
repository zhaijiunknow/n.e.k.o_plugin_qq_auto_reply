# -*- coding: utf-8 -*-
"""控制台做减法（2026-11-16）：很多用户是学生，不该一进来面对八个入口。

只动布局、不删功能：侧栏只留日常三项，其余收进默认折叠的「高级」；名单页里排查工具和
认领码收进折叠区；没换过通道的人看不到「换通道认领」。
"""

from __future__ import annotations

import pathlib
import re

HTML = (pathlib.Path(__file__).resolve().parents[1] / "static" / "open_platform.html").read_text(encoding="utf-8")
NAV = HTML[HTML.index('<nav aria-label="控制台导航">'):HTML.index("</nav>")]


def _pages(fragment: str) -> list[str]:
    return [page + (":" + sub if sub else "") for page, sub in
            re.findall(r'class="nav-item[^"]*" data-page="([a-z]+)"(?: data-sub="([a-z]+)")?', fragment)]


def test_the_sidebar_shows_three_everyday_entries():
    everyday, advanced = NAV.split('<details class="nav-advanced"')
    assert _pages(everyday) == ["status", "config:accounts", "sticker"]
    assert _pages(advanced) == ["config", "plugintools", "prompts", "logs", "guide"]
    assert 'data-nav="status.html"' in advanced, "重新扫码绑定的入口还在（收在高级里）"
    assert "<details" in NAV and " open" not in NAV.split("<summary")[0].split("<details")[1], "高级默认折叠"


def test_every_page_is_still_reachable():
    for page in ("status", "config", "sticker", "plugintools", "prompts", "logs", "guide"):
        assert f'data-page="{page}"' in NAV


def test_the_roster_entry_opens_the_accounts_tab_and_highlights_alone():
    assert "switchPage(item.dataset.page);if(item.dataset.sub){switchSub(item.dataset.sub);adv.open=was}" in HTML, (
        "「名单」要直接落到用户子页，并且不因为途经「配置」的第一个子页而把「高级」展开"
    )
    assert re.search(r"^function markNav\(\)", HTML, re.M)
    switch_sub = re.search(r"^function switchSub\(.*$", HTML, re.M).group(0)
    assert switch_sub.rstrip().endswith("markNav()}"), "每次换页 / 换子页都要重算侧栏高亮"
    assert "adv.open=true" in HTML, "当前页在高级里时要自动展开"


def test_the_accounts_page_tucks_rarely_used_tools_away():
    page = HTML.split('<div class="page" id="page-config-accounts">')[1].split('<div class="page"')[0]
    probe = page.index('id="cfg-identity-probe"')
    assert page.rindex("<details", 0, probe) > page.rfind("</details>", 0, probe), "排查开关在折叠区里"
    card = page[page.index('id="identity-links-card"'):page.index('id="identity-scope-card"')]
    assert card.index('id="seen-table"') < card.index("<details") < card.index('id="link-table"'), (
        "先是「这是谁」的列表，认领码和已匹配记录收在折叠区"
    )
    assert 'id="identity-links-card" style="display:none"' in page
    assert "getElementById('identity-links-card').style.display=" in HTML, "名单里没有旧账号时整张卡片不出现"
