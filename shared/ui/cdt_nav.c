/*
 * cdt_nav.c — KEY 导航纯逻辑实现（P2.2，A2）
 * 规则真源见 cdt_nav.h 头注；本文件不得 include LVGL/SDL/ESP 头。
 */
#include <stddef.h>

#include "cdt_nav.h"

static uint8_t clamp_sub(uint8_t idx, uint8_t pages)
{
    if (pages == 0) return 0;
    return (idx >= pages) ? (uint8_t)(pages - 1u) : idx;
}

void cdt_nav_init(cdt_nav_t *nav, cdt_page_t page)
{
    if (nav == NULL) return;
    nav->page = (page == CDT_PAGE_NOW || page == CDT_PAGE_AGENTS ||
                 page == CDT_PAGE_PLAN || page == CDT_PAGE_USAGE)
                    ? page
                    : CDT_PAGE_NOW;
    nav->agents_page = 0;
    nav->plan_page = 0;
}

bool cdt_nav_clamp(cdt_nav_t *nav, const cdt_view_t *view)
{
    bool changed = false;

    if (nav == NULL) return false;
    if (nav->page != CDT_PAGE_AGENTS && nav->page != CDT_PAGE_PLAN &&
        nav->page != CDT_PAGE_USAGE && nav->page != CDT_PAGE_NOW) {
        nav->page = CDT_PAGE_NOW; /* 防御：普通页选择里不允许出现强制页 */
        changed = true;
    }

    if (view == NULL) {
        if (nav->agents_page != 0 || nav->plan_page != 0) changed = true;
        nav->agents_page = 0;
        nav->plan_page = 0;
        return changed;
    }

    {
        uint8_t a = clamp_sub(nav->agents_page, view->agents_pages);
        uint8_t p = clamp_sub(nav->plan_page, view->plan_pages);
        if (a != nav->agents_page) { nav->agents_page = a; changed = true; }
        if (p != nav->plan_page) { nav->plan_page = p; changed = true; }
    }
    return changed;
}

uint32_t cdt_nav_key(cdt_nav_t *nav, const cdt_view_t *view, cdt_key_event_t ev)
{
    uint8_t agents_pages = 1, plan_pages = 1;

    if (nav == NULL) return CDT_NAV_ACT_NONE;
    if (view != NULL) {
        agents_pages = view->agents_pages;
        plan_pages = view->plan_pages;
    }

    /* 长按：只静音当前提醒，任何页（含强制页）都不切页（§6 KEY 行） */
    if (ev == CDT_KEY_LONG_PRESS) {
        return CDT_NAV_ACT_MUTE;
    }
    if (ev != CDT_KEY_SHORT_PRESS) {
        return CDT_NAV_ACT_NONE;
    }

    /* 短按 + 低压强制页：拒绝普通页切换（P2.3 验收；nav 保持原值，
     * 强制解除后Presenter 自然回落到 nav.page —— 恢复保留普通页面） */
    if (view != NULL && view->low_battery_forced) {
        return CDT_NAV_ACT_NONE;
    }

    switch (nav->page) {
        case CDT_PAGE_AGENTS:
            if (nav->agents_page + 1u < agents_pages) {
                nav->agents_page++; /* 先推进子页（P2 固定行为） */
                return CDT_NAV_ACT_NONE;
            }
            nav->page = CDT_PAGE_PLAN;
            nav->plan_page = 0;
            return CDT_NAV_ACT_PAGE;

        case CDT_PAGE_PLAN:
            if (nav->plan_page + 1u < plan_pages) {
                nav->plan_page++;
                return CDT_NAV_ACT_NONE;
            }
            nav->page = CDT_PAGE_USAGE;
            return CDT_NAV_ACT_PAGE;

        case CDT_PAGE_USAGE:
            nav->page = CDT_PAGE_NOW;
            return CDT_NAV_ACT_PAGE;

        case CDT_PAGE_NOW:
        default: /* INVALID/异常值归一为 NOW 后轮换 */
            nav->page = CDT_PAGE_AGENTS;
            nav->agents_page = 0;
            return CDT_NAV_ACT_PAGE;
    }
}
