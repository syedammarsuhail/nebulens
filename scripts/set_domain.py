"""Point NebuLens (website, pricing links, licence contact) at your own domain.

    python scripts/set_domain.py nebulens.io            # site at https://nebulens.io/
    python scripts/set_domain.py www.nebulens.io        # site at https://www.nebulens.io/

Rewrites every https://syedammarsuhail.github.io/nebulens/ link (product, docs, README, guide) to the new domain,
changes the licence contact address to licensing@<apex domain>, and writes docs/CNAME so GitHub Pages serves the
site there. Then add the DNS records it prints at your registrar and set the custom domain under
GitHub > Settings > Pages (tick "Enforce HTTPS" once the certificate is issued).
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OLD_SITE = "https://syedammarsuhail.github.io/nebulens/"
FILES = ["server.py", "static/core.js", "README.md", "GETTING-STARTED.md", "UPGRADE.md", "LICENSE"] + \
        [os.path.join("docs", f) for f in sorted(os.listdir(os.path.join(ROOT, "docs"))) if f.endswith(".html")]


def main():
    if len(sys.argv) != 2 or not re.fullmatch(r"(?=.{4,253}$)([a-z0-9-]+\.)+[a-z]{2,}", sys.argv[1].lower()):
        sys.exit(__doc__)
    host = sys.argv[1].lower()
    apex = host[4:] if host.startswith("www.") else host
    new_site = f"https://{host}/"
    changed = []
    for rel in FILES:
        p = os.path.join(ROOT, rel)
        if not os.path.exists(p):
            continue
        s = open(p, encoding="utf-8", newline="").read()
        t = s.replace(OLD_SITE, new_site)
        t = re.sub(r"licensing@[a-z0-9.-]+\.[a-z]{2,}", f"licensing@{apex}", t)
        if t != s:
            open(p, "w", encoding="utf-8", newline="").write(t)
            changed.append(rel)
    with open(os.path.join(ROOT, "docs", "CNAME"), "w", encoding="utf-8", newline="\n") as f:
        f.write(host + "\n")
    changed.append("docs/CNAME")
    print("updated:", ", ".join(changed))
    print(f"\nDNS records to add at your registrar for {apex}:")
    for ip in ("185.199.108.153", "185.199.109.153", "185.199.110.153", "185.199.111.153"):
        print(f"  A      @     {ip}")
    print("  CNAME  www   syedammarsuhail.github.io")
    print(f"\nThen GitHub > Settings > Pages > Custom domain: {host}, and Enforce HTTPS once it is offered.")


if __name__ == "__main__":
    main()
