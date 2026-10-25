# Third-party notices

Files or values copied from third-party projects are listed here with their license.

No third-party code is copied into this repository. The only copied values are the name lists of `src/generator/names.py`, drawn once from Faker; the other projects below are tools it depends on.

| Project | Use | License |
|---|---|---|
| [Faker](https://github.com/joke2k/faker) 40.43.0, Copyright (c) 2012 Daniele Faraglia | Male first names and surnames of 12 European locales, drawn once by `scripts/draw_names.py` and frozen in `src/generator/names.py`; Faker is not a dependency of the code that runs | MIT |
| [common-data-format-validator](https://github.com/UnravelSports/common-data-format-validator), Copyright (c) 2025 UnravelSports | Test dependency: checks the Common Data Format export against the official CDF JSON schemas | MIT |

The Common Data Format is described in Anzer, G., Arnsmeyer, K., Bauer, P., Bekkers, J., Brefeld, U., Davis, J., Evans, N., Kempe, M., Robertson, S. J., Smith, J. W., & Van Haaren, J. (2025), *Common Data Format (CDF): a Standardized Format for Match-Data in Football (Soccer)*. The action types of the feed follow SPADL (Decroos et al., KDD 2019), whose reference implementation, socceraction, is MIT-licensed; none of its code is used.
