# Artwork for home-assistant/brands

Home Assistant does not read an integration's logo from the integration. The
frontend fetches it from `brands.home-assistant.io` by domain, which is served
from the [home-assistant/brands](https://github.com/home-assistant/brands)
repository — so until the artwork is submitted there, EZVIZ Doorbell shows the
generic puzzle piece, however many images sit next to the code.

`custom_integrations/ezviz_doorbell/` here holds exactly what that repository
asks for, built from `assets/` by `tools/make_icons.py`:

| File | Size |
| --- | --- |
| `icon.png` | 256×256 |
| `icon@2x.png` | 512×512 |
| `logo.png` | 512×171 |
| `logo@2x.png` | 1024×341 |

## Submitting them

The brands repository takes custom integrations under `custom_integrations/`,
named by domain. From a fork of it:

```bash
mkdir -p custom_integrations/ezviz_doorbell
cp path/to/ha-ezviz-doorbell/brands/custom_integrations/ezviz_doorbell/*.png \
   custom_integrations/ezviz_doorbell/
```

Open a pull request from there. Their checks want PNGs, square icons, a logo no
more than 256 px tall, and no whitespace around the edges; these are built to
those rules. Once it is merged the icon appears on its own — nothing has to be
released here, and nobody has to update anything.
