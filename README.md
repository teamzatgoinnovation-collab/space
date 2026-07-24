# Space

Frappe Cloud-like control plane for ZatGo Innovation.

- **Package:** `space`
- **Control site:** `space.zatgo.online`
- **Portal:** `portal.zatgo.online` (space-web)

## Install

```bash
bench get-app https://github.com/teamzatgoinnovation-collab/space.git --branch main
bench --site space.zatgo.online install-app space
bench --site space.zatgo.online migrate
bench --site space.zatgo.online clear-cache
```
