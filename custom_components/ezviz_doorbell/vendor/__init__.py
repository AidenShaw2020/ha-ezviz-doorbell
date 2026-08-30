"""Third party code shipped with this integration.

``pyezvizapi`` lives here rather than being installed, because Home Assistant's
built-in EZVIZ integration pins version 1.0.0.7 of it and this integration
needs 1.0.5.0 - the one that can open the push channel, take a picture on
demand and carry the cloud video stream. There is one set of site-packages
between them, so whichever integration is set up last decides which version is
on disk, and the other one breaks. Carrying our own copy is what lets both be
installed at once.

The copy is unmodified pyezvizapi 1.0.5.0, Apache-2.0 licensed, from
https://github.com/RenierM26/pyEzvizApi - its LICENSE sits beside it. Nothing
here is edited; to update it, replace the directory with a newer release.
"""
