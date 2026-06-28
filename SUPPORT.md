# Support And Maintenance Policy

This repository is a working reference prototype released under the MIT
license. It is meant to show a practical local bridge pattern, API surface,
console, CLI, Docker setup, and demo use cases that other developers can study,
fork, adapt, and build on.

ภาษาไทย: โปรเจกต์นี้เป็น reference prototype ภายใต้ MIT license จุดประสงค์คือ
โชว์แนวทางการทำ local bridge, API, console, CLI, Docker และตัวอย่าง use case
จริง เพื่อให้คนอื่นศึกษา fork และเอาไปต่อยอดได้

## Active Support Window

I will help answer issues, review pull requests, and fix necessary project
problems until **June 30, 2026 (30/6/69 B.E.)**.

During this window, the most useful issues and PRs are:

- reproducible setup, Docker, CLI, or documentation problems;
- clear bugs with logs, commands, screenshots, or request/response examples;
- small fixes that make the project easier for other people to run;
- security or privacy improvements;
- documentation improvements that reduce confusion for new users.

ภาษาไทย: ผมจะช่วยตอบ issue, ดู PR, และแก้ปัญหาที่จำเป็นให้ถึง
**30 มิถุนายน 2026 (30/6/69)** เท่านั้น ช่วงนี้เหมาะกับ issue/PR ที่มีข้อมูล
ชัดเจน เช่น setup พัง, Docker/CLI/docs มีปัญหา, bug ที่ reproduce ได้, security
improvement หรือ docs ที่ช่วยให้คนใหม่ใช้ง่ายขึ้น

## After June 30, 2026

After this date, the repository will remain public and MIT-licensed. You can
still fork it, modify it, use it, open issues, or submit pull requests.

However, I do not plan to provide guaranteed ongoing support, continuous feature
development, or product-grade maintenance in this repository after the active
support window.

The reason is simple: I have started a new personal main project that uses some
of the same ideas and architecture patterns. It is my own work, not work owned
by or assigned through any company. If I keep turning this reference
implementation into a continuously maintained product-grade codebase, it may
conflict with my own future work and business interests. I want to keep that
boundary clear.

This repository should be treated as an open-source reference implementation,
not as a product that I will maintain indefinitely.

ภาษาไทย: หลังวันที่ 30 มิถุนายน 2026 repo นี้ยัง public และยังเป็น MIT license
เหมือนเดิม ทุกคนยัง fork แก้ ใช้ เปิด issue หรือส่ง PR ได้ แต่ผมจะไม่รับประกัน
ว่าจะตอบ issue, review PR, เพิ่ม feature หรือดูแลต่อเนื่อง เพราะผมเพิ่งเริ่มทำ
โปรเจกต์ใหม่ส่วนตัวที่เป็นงานหลักของผมเองและใช้แนวคิดหรือ architecture บางส่วน
ร่วมกัน โปรเจกต์ใหม่นี้ไม่ได้อยู่ภายใต้บริษัทไหน เป็นงานที่ผมทำเอง ถ้าอัปเดต
repo นี้เรื่อย ๆ จนเป็น product-grade implementation เต็มรูปแบบ อาจทับกับงานใหม่
และผลประโยชน์ของผมเอง ดังนั้น repo นี้ควรถูกมองเป็น reference implementation
ไม่ใช่ product ที่ผมจะดูแลแบบไม่มีขอบเขต

## Paid Work

If this project is useful to you, your company, or your product, and you want a
more polished implementation, production hardening, custom integration, hosted
deployment, real billing, or long-term maintenance, please contact me for paid
work.

Contact:

- Email: `suphotprathumchat@gmail.com`
- Facebook: <https://facebook.com/max.266318>

This open-source release is intentionally a reference implementation. Real
production systems still need additional work around authentication, tenant
isolation, secret storage, durable queues, observability, abuse controls,
billing reconciliation, and deployment hardening.

ภาษาไทย: ถ้าโปรเจกต์นี้มีประโยชน์กับคุณ บริษัทของคุณ หรือ product ของคุณ และ
ต้องการเวอร์ชันที่จริงจังกว่านี้ เช่น production hardening, custom integration,
hosted deployment, billing จริง หรือ long-term maintenance สามารถติดต่อผมเพื่อ
จ้างงานได้

ช่องทางติดต่อ:

- Email: `suphotprathumchat@gmail.com`
- Facebook: <https://facebook.com/max.266318>

โค้ดใน repo นี้ตั้งใจเปิดเป็นตัวอย่างและจุดเริ่มต้น งาน production จริงยังควร
เพิ่ม auth, tenant isolation, secret storage, durable queue, observability,
abuse control, billing reconciliation และ deployment hardening
