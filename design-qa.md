# Globuy Product Design QA

## Long-term memory center redesign (2026-07-21)

- source visual truth path: `C:\Users\wenkx\AppData\Local\Temp\codex-clipboard-3c04b84e-bab9-4274-9dba-869c164ed99c.png`
- implementation screenshots: `output/account-memory-desktop.png`, `output/account-memory-mobile.png`
- side-by-side comparison: `output/account-memory-comparison.png`
- browser: installed Google Chrome at 1440×900 and 390×844 CSS px

**Findings**

- The requested information architecture is implemented: long-term memories occupy the left card, while the right card contains only quick choices and the add-memory form. Wishlist content is no longer duplicated on this page.
- Six quick tags are visible and interactive. Selecting “中国风” populated `chinese_style` and its editable Chinese preference sentence while preserving the existing memory category contract.
- The existing warm paper palette, rounded editorial cards, Newsreader/Noto Serif SC headings, DM Sans/Noto Sans SC UI text, spacing, borders, and icon library remain consistent with the supplied screen.
- Desktop and mobile layouts have no horizontal overflow. The mobile account header uses a dedicated two-row arrangement, and the memory cards stack without clipping.

**Interaction and data checks**

- Chrome DOM verification: `heading=你的长期记忆`, `memories=2`, `tags=6`, selected quick-memory key `chinese_style`.
- Real MySQL verification imported 1000 verified snapshot offers and completed one isolated wishlist add/read round trip; the temporary QA user was deleted afterward.
- Backend: 134 tests passed. Frontend: 4 files / 13 tests passed. TypeScript and Vite production build passed.
- No paid model or external shopping provider was called.

final result: passed

---

## Registration typography correction (2026-07-21)

- source visual truth path: `C:\Users\wenkx\AppData\Local\Temp\codex-clipboard-a2a81e12-800d-4d03-afef-86d0eeb4aae3.png`
- implementation screenshot path: `output/design-qa/auth-register-typography-final.png`
- viewport: 2048 × 1080 CSS px in installed Google Chrome
- state: registration tab with all four fields filled
- full-view comparison evidence: `output/design-qa/auth-register-typography-comparison.png`
- focused comparison evidence: `output/design-qa/auth-register-typography-focus.png`

**Findings**

- No actionable P0, P1, or P2 visual issue remains for the three annotated registration-panel corrections.
- The display-name, email, password, confirmation, password-rule, tab, and submit-button typography is larger and easier to scan without changing the panel width.
- The registration explanation below the heading has been removed from both markup and layout.
- `创建你的账号。` now uses the existing Newsreader/Noto Serif SC display stack at a smaller responsive scale and is locked to one line at the supplied desktop width.
- The left illustration, lower-left `Globuy` wordmark, color tokens, field order, and authentication behavior are unchanged.

**Comparison History**

- Pass 1: the annotated source identified P2 readability issues in the form, an unwanted explanatory line, and a P2 heading wrap that isolated the final characters.
- Pass 2: Chrome renders the heading on one line, omits the explanation, and reports 12 px labels, 14 px input text, and 11 px password-rule text with no horizontal overflow.

**Interaction and Console Checks**

- Login/register tab switching and all registration fields remain interactive; the test filled all four fields through native input events.
- Chrome DOM checks confirmed `authTitleSingleLine=true`, `authDescriptionRemoved=true`, and `noHorizontalOverflow=true`.
- The intercepted unauthenticated `/auth/me` request produces the expected 401 console resource entry; no JavaScript runtime exception was found.
- TypeScript compilation passed. Vitest passed 4 files / 12 tests. Vite 7.3.6 production build passed.

final result: passed

---

## Shipping and snapshot field removal (2026-07-21)

- source visual truth paths: `C:\Users\wenkx\AppData\Local\Temp\codex-clipboard-2896f304-22a6-41a7-9723-2927f0c45d63.png`, `C:\Users\wenkx\AppData\Local\Temp\codex-clipboard-0ce7e391-fc5b-4069-81ba-455dac188e25.png`, `C:\Users\wenkx\AppData\Local\Temp\codex-clipboard-0aea0b2f-2849-4284-b931-d822d0c0fba1.png`
- implementation screenshot path: `output/qa-no-shipping-fields.png`
- browser: installed Google Chrome against `http://127.0.0.1:5173/`

**Findings**

- Product cards no longer show shipping, postage, free-shipping, or “pending confirmation” placeholders when the source data has no shipping fact.
- The result heading no longer carries an “offline data snapshot” badge, and the generic data-note and bottom snapshot disclaimer are absent.
- Historic assistant Markdown is sanitized at render time, so persisted sessions receive the same presentation rule after refresh without rewriting stored messages.
- Non-shipping warnings remain visible; the removal is narrowly scoped to the unsupported shipping and snapshot-copy fields.

**Verification**

- Chrome DOM audit: `hasShipping=false`, `hasDataNote=false`, `hasSnapshotBadge=false`, `hasSnapshotDisclaimer=false`, `cards=3`.
- Backend targeted suite: 34 passed. Frontend: 4 files / 12 tests passed. Ruff and Vite production build passed.
- Full backend suite reached 116 passed and 12 unrelated API isolation failures caused by prior test state returning HTTP 422; the API suite passes when run in isolation.
- No paid model or shopping-provider call was made.

final result: passed

---

## Composer anchoring and real product images (2026-07-21)

- source visual truth paths: `C:\Users\wenkx\Documents\xwechat_files\wxid_e76k6kt8dwli22_6b57\temp\RWTemp\2026-07\9e20f478899dc29eb19741386f9343c8\b10afdde63a7c4849dcc899c6f855d37.png`, `C:\Users\wenkx\Documents\xwechat_files\wxid_e76k6kt8dwli22_6b57\temp\RWTemp\2026-07\9e20f478899dc29eb19741386f9343c8\6144d8a603516c0f5a7817acabb1d996.png`
- implementation screenshot paths: `output/qa-composer-bottom.png`, `output/qa-real-product-images.png`
- viewport: 2048 × 1080 in installed Google Chrome

**Findings**

- No actionable P0, P1, or P2 layout issue remains for the composer anchoring request.
- The conversation panel now uses a vertical flex layout. The message surface consumes the available height and the composer remains the final non-growing child, independent of whether an active or archived header is present.
- Chrome measured the active conversation panel bottom at 950 px and the composer bottom at 949 px; the 1 px difference is the panel border, so the composer is visually bottom-anchored.
- Existing stored results with missing `image_url` are enriched at read time from the verified local headphone snapshot. New ShoppingSummary results are enriched before persistence as well.
- The three products shown in the supplied mock-image screenshot resolve to their matching dataset URLs by platform and item ID. Chrome loaded the Q45, Space One Pro, and Bose URLs successfully at 350 × 350 natural pixels.
- The hand-drawn image remains only as an explicit error fallback for missing or externally unreachable URLs; it is no longer the normal data source for those products.

**Verification**

- Backend API inspection confirmed non-null `image_url` values for the three legacy picks without rewriting their stored source facts.
- Backend: 126 tests passed. Frontend: 3 files / 9 tests passed. TypeScript and Vite production build passed.
- Automated tests did not call a paid model or shopping provider.

final result: passed

---

## Landing hero two-line title update (2026-07-21)

- source visual truth path: `C:\Users\wenkx\AppData\Local\Temp\codex-clipboard-5d4fec66-f58d-45bb-a476-b15d7b5a64fd.png`
- normalized source path: `output/design-qa/landing-two-line-source.png`
- implementation screenshot path: `output/design-qa/landing-two-line-title.png`
- viewport: 1414 × 1266 CSS px in installed Google Chrome
- state: landing page
- full-view comparison evidence: `output/design-qa/landing-two-line-comparison.png`
- focused region comparison evidence: `output/design-qa/landing-two-line-focus.png`

**Findings**

- No actionable P0, P1, or P2 visual differences remain for the requested title-wrap correction.
- The annotated source shows the two intended phrases splitting again inside each phrase, producing four visual rows. The implementation renders exactly two rows: `找到更适合` and `你的商品。`.
- The title retains the Newsreader/Noto Serif SC display stack, regular weight, editorial tracking, and existing left alignment. Only the responsive size and per-line wrapping behavior changed.
- Each intended line is now a block with `white-space: nowrap`; the reduced `clamp(2.375rem, 2.75vw, 4rem)` scale fits the current narrow column while the existing mobile override remains readable.
- Spacing/layout rhythm, colors/tokens, image quality, copy, buttons, and the lower-left `Globuy` wordmark are unchanged.

**Comparison History**

- Pass 1: the user-provided Chrome capture showed a P2 headline-wrap issue: the intended two-line headline appeared as four rows with isolated characters.
- Pass 2: after reducing the responsive size and locking the two semantic line spans, Chrome rendered exactly two rows with no clipping or horizontal page overflow.

**Interaction and Console Checks**

- The primary and secondary landing actions remain unchanged.
- Chrome DOM checks confirmed two headline line boxes with the intended texts, both lines fitting their containers, and no horizontal page overflow.
- Chrome reported no JavaScript runtime exception. The landing preview still requests the existing missing favicon and records one handled 404 unrelated to this title change.
- Vitest: 3 files / 7 tests passed. TypeScript and Vite production build passed.

final result: passed

---

## Typography and landing-canvas update (2026-07-21)

- visual baseline paths: `output/design-qa/landing-before-fonts.png`, `output/design-qa/workbench-before-fonts.png`
- implementation screenshot paths: `output/design-qa/landing-fonts.png`, `output/design-qa/workbench-fonts.png`
- combined comparison paths: `output/design-qa/landing-font-comparison.png`, `output/design-qa/workbench-font-comparison.png`
- focused typography comparison: `output/design-qa/landing-font-focus.png`
- viewport: landing 2046 × 1071 CSS px at DPR 1.25; workbench 2047 × 1084 CSS px at DPR 1.25

**Findings**

- No actionable P0, P1, or P2 visual difference remains for the requested typography and canvas-color update.
- Display headings use the Newsreader stack, with Noto Serif SC supplying Chinese glyphs; UI copy and controls use DM Sans, with Noto Sans SC supplying Chinese glyphs.
- The landing hero is regular weight with editorial spacing and a responsive size constrained to the narrow right column. The first exact 7vw render was visually too large for this layout and was corrected before the final capture.
- The oversized lower-left `Globuy` wordmark still resolves to its original Trebuchet MS stack and is visually unchanged.
- The landing art canvas uses the sampled dominant outer-edge color of the source WebP, `#FDF8EA`, and the prior multiply blend is removed so the source colors meet the canvas without a tinted rectangular seam.
- The before/implementation combined images confirm the hierarchy has moved from a generic bold UI treatment toward serif display headings plus sans-serif UI while preserving the existing layout, actions, illustration scale, and responsive shell.

**Browser and interaction checks**

- Installed Google Chrome loaded all four bundled variable font families successfully.
- Computed styles confirmed display, UI, button, input, and preserved-wordmark font stacks; the sampled art background resolved to `rgb(253, 248, 234)` and the illustration blend mode to `normal`.
- Landing and workbench had no horizontal overflow and Chrome reported no JavaScript runtime exception.
- Vitest: 3 files / 7 tests passed. TypeScript and Vite production build passed.

final result: passed

---

- source visual truth path: `C:\Users\wenkx\AppData\Local\Temp\codex-clipboard-4fea85d5-8a1a-423f-b947-f9c6d3b2233e.png`
- source workbench truth path: `C:\Users\wenkx\AppData\Local\Temp\codex-clipboard-6be2f129-3610-4672-b592-06759ed4c135.png`
- implementation screenshot paths: `output/design-qa/landing-revised.png`, `output/design-qa/workbench-revised.png`
- combined comparison paths: `output/design-qa/landing-comparison.png`, `output/design-qa/workbench-comparison.png`
- viewport: landing 2046 × 1071 CSS px at DPR 1.25; workbench 2047 × 1084 CSS px at DPR 1.25
- state: landing; empty active conversation with the backend-error banner visible

**Findings**

- No actionable P0, P1, or P2 visual differences remain for the annotated change.
- The landing status/version footer is absent, and the headline, description, and two actions form one group higher in the right column.
- The workbench no longer shows the top current-view label, left brand heading, duplicate left new-session button, active-conversation heading, or local SQLite footer.
- The top-right new-session action remains available, including as the compact mobile action, so the removal does not break the core new-session path.

**Required Fidelity Surfaces**

- Fonts and typography: existing families, sizes, weights, line heights, wrapping, and hierarchy are unchanged outside the deleted labels.
- Spacing and layout rhythm: the landing entry group moves upward without changing its internal rhythm; the workbench search and active-session content now start at the top of the left panel, and the empty conversation uses the released header space.
- Colors and visual tokens: no palette, border, radius, shadow, or semantic-color changes were introduced.
- Image quality and asset fidelity: the supplied WebP illustration and brand mark retain their original crop, scale, and colored-pencil treatment.
- Copy and content: only user-circled content was removed. Functional session, archive, composer, status, and runtime copy remains.

**Full-view Comparison Evidence**

- `output/design-qa/landing-comparison.png` places the annotated landing reference and revised Chrome capture in one image.
- `output/design-qa/workbench-comparison.png` places the annotated workbench reference and revised Chrome capture in one image.
- Both captures were made with the installed Google Chrome using isolated temporary profiles. DOM checks confirmed every requested selector was removed and no horizontal overflow was present.

**Focused Region Comparison Evidence**

- Landing right column: footer/status content is gone and the entry group begins substantially higher while retaining the two existing actions.
- Workbench chrome: the top center area is empty, the left panel begins with search, and the active conversation no longer renders its header block.
- Workbench lower-left: no privacy/storage footer remains.

**Comparison History**

- Pass 1: the earlier build had no browser-rendered evidence and remained blocked.
- Pass 2: Chrome full-view captures showed all marked removals in place. The first landing capture was taken before image compositing completed, so it was recaptured without GPU suppression and with a virtual-time budget.
- Pass 3: captures were repeated at the source screenshots' effective CSS viewport and DPR, then recombined for like-for-like comparison. No P0/P1/P2 issue remained.

**Interaction and Console Checks**

- Vitest verified all 7 existing component/state tests, including the new-session dialog keyboard behavior.
- Chrome DOM checks confirmed the top new-session button remains available after removing the duplicate left action.
- Chrome reported no JavaScript runtime exception. The preview recorded the existing handled API 500 response in the offline/error state and a missing favicon request; neither is caused by this layout change.

**Follow-up Polish**

- None required for this annotated change.

final result: passed
## Authentication and wishlist frontend adaptation (2026-07-21)

- Desktop authentication cover: `output/design-qa/auth-landing-final.png` at 1440×1000.
- Desktop wishlist: `output/design-qa/wishlist-desktop-final.png` at 1440×1000.
- Mobile wishlist: `output/design-qa/wishlist-mobile-final.png` at 390×844.
- Chrome DOM checks passed for all three captures: correct route and no horizontal document overflow.
- The authentication form preserves the original cover composition and illustration background; the wishlist reuses the established warm paper palette, typography, restrained borders, and product fallback artwork.
- Desktop and mobile wishlist checks covered the heading, product row/card adaptation, current/add price hierarchy, change state, freshness metadata, external link action, price-details action, and 44px mobile controls.
- The two side illustrations are standalone inline SVG components and become low-opacity background details; they are hidden on small screens.
- The local backend health endpoint timed out during QA. CDP fulfilled documented auth/wishlist response shapes only for visual rendering; these screenshots do not claim successful MySQL or CSRF integration.
- Result: visual and responsive QA passed; live backend integration remains environment-blocked until port 8000 responds.
