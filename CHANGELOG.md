# Changelog

## [0.0.6](https://github.com/bakerkj/ha-vigil/compare/v0.0.5...v0.0.6) (2026-08-08)


### Miscellaneous Chores

* **deps:** update dependency eslint to v10.8.1 ([#45](https://github.com/bakerkj/ha-vigil/issues/45)) ([29d74d8](https://github.com/bakerkj/ha-vigil/commit/29d74d81257dc3b895601e118d4df6d7b9a2e96c))
* **deps:** update dependency uv to v0.12.3 ([#46](https://github.com/bakerkj/ha-vigil/issues/46)) ([a4a88b3](https://github.com/bakerkj/ha-vigil/commit/a4a88b3a33b7516539e714c603302136c3d04b5c))
* **deps:** update pre-commit hook astral-sh/ruff-pre-commit to v0.16.2 ([#43](https://github.com/bakerkj/ha-vigil/issues/43)) ([96db997](https://github.com/bakerkj/ha-vigil/commit/96db9979b3fb4d85dc171fd13b2b80894cbda266))

## [0.0.5](https://github.com/bakerkj/ha-vigil/compare/v0.0.4...v0.0.5) (2026-08-08)


### Bug Fixes

* import StaticPathConfig compatibly across HA 2026.7/2026.8 ([#42](https://github.com/bakerkj/ha-vigil/issues/42)) ([4b31656](https://github.com/bakerkj/ha-vigil/commit/4b31656493cd14b43e02ca3d7febc966b8789e23))
* **pre-commit:** set default_stages so hooks skip commit-msg by default ([#39](https://github.com/bakerkj/ha-vigil/issues/39)) ([032dbe3](https://github.com/bakerkj/ha-vigil/commit/032dbe387dd0a6f3bd0374a9473273a92570e11f))


### Miscellaneous Chores

* **deps:** pin uv to 0.12.2 ([#38](https://github.com/bakerkj/ha-vigil/issues/38)) ([c8c2ee1](https://github.com/bakerkj/ha-vigil/commit/c8c2ee11b9bd913a6d13fa1002f611b1dcb0ce83))
* **deps:** update anthropics/claude-code-action action to v1.0.184 ([#40](https://github.com/bakerkj/ha-vigil/issues/40)) ([ef55936](https://github.com/bakerkj/ha-vigil/commit/ef55936ac4edd0f09f8b552a110b941d93746049))
* **deps:** update dependency globals to v17.8.0 ([4333a97](https://github.com/bakerkj/ha-vigil/commit/4333a97e86520fbf437be3f7203ffe8709727762))
* **deps:** update dependency globals to v17.8.0 ([e6efbc3](https://github.com/bakerkj/ha-vigil/commit/e6efbc3c3ee4b034dc477a222c7ee0c8e621f89e))
* **deps:** update dependency globals to v17.9.0 ([#36](https://github.com/bakerkj/ha-vigil/issues/36)) ([6989154](https://github.com/bakerkj/ha-vigil/commit/6989154014a32eeb836e37fae243d08d963bdd22))
* **deps:** update dependency homeassistant to ==2026.8.* ([#41](https://github.com/bakerkj/ha-vigil/issues/41)) ([5d97c5a](https://github.com/bakerkj/ha-vigil/commit/5d97c5a2749ac64df6a4ca14ca8dcdf918bb509a))
* **deps:** update dependency jsdom to v30 ([5d7b31d](https://github.com/bakerkj/ha-vigil/commit/5d7b31d8413a93b8a82bc070e3ef761c52ea158e))
* **deps:** update dependency jsdom to v30 ([9a53a6f](https://github.com/bakerkj/ha-vigil/commit/9a53a6f31ee35360acf35488e58fd5824eaa97be))
* **deps:** update dependency jsdom to v30.0.1 ([93b91b0](https://github.com/bakerkj/ha-vigil/commit/93b91b09fa4d8e279e6e5a7a11aa2dbceb4d2a9b))
* **deps:** update dependency jsdom to v30.0.1 ([506367b](https://github.com/bakerkj/ha-vigil/commit/506367b4b5fca0be6269d481e0fec22500591c89))
* **deps:** update dependency uv to ==0.12.* ([66af836](https://github.com/bakerkj/ha-vigil/commit/66af83639cc712e043c2c94bdaf53155fe0b06f7))
* **deps:** update dependency uv to ==0.12.* ([49be76e](https://github.com/bakerkj/ha-vigil/commit/49be76e341fa8306e682166468709e48bc3d76fa))
* **deps:** update home-assistant/actions digest to a7c616c ([#37](https://github.com/bakerkj/ha-vigil/issues/37)) ([83aee43](https://github.com/bakerkj/ha-vigil/commit/83aee4329e27ec0a8e5a14c5bed28ba43f0b7f00))
* **deps:** update home-assistant/actions digest to ab22029 ([81e80a7](https://github.com/bakerkj/ha-vigil/commit/81e80a73245218698105c5e8ffa3a6a21f3516e0))
* **deps:** update home-assistant/actions digest to ab22029 ([71f1625](https://github.com/bakerkj/ha-vigil/commit/71f162550e9c1452395af09df379561a567f1c5e))
* **deps:** update j178/prek-action action to v3 ([11cb52d](https://github.com/bakerkj/ha-vigil/commit/11cb52dc2a40120e8cefe2f63ed5ed1d35dad133))
* **deps:** update j178/prek-action action to v3 ([2f8bfe3](https://github.com/bakerkj/ha-vigil/commit/2f8bfe391b919b845f90474fadf980dff98ecd8a))
* **deps:** update pre-commit hook astral-sh/ruff-pre-commit to v0.16.1 ([00cee1e](https://github.com/bakerkj/ha-vigil/commit/00cee1e869034bca1034e915051e11e76332725e))
* **deps:** update pre-commit hook astral-sh/ruff-pre-commit to v0.16.1 ([b4e9438](https://github.com/bakerkj/ha-vigil/commit/b4e9438a1f8065ba44b21da6ef7a1ba4627f1c3e))


### Tests

* fix UTC-midnight flake in recorder aggregate tests ([bbcedd4](https://github.com/bakerkj/ha-vigil/commit/bbcedd45dceb01263566486199c305c28dc94891))
* pin recorder aggregate tests off UTC midnight to kill a boundary flake ([9545cc5](https://github.com/bakerkj/ha-vigil/commit/9545cc5851eff384dc27e2704c879f44331d86de))


### Continuous Integration

* enable renovate auto-merge for CI-only updates ([#35](https://github.com/bakerkj/ha-vigil/issues/35)) ([62c20d8](https://github.com/bakerkj/ha-vigil/commit/62c20d8ee70eaa067dd9c94ac1097eff3d43c3a3))

## [0.0.4](https://github.com/bakerkj/ha-vigil/compare/v0.0.3...v0.0.4) (2026-07-26)


### Bug Fixes

* use toml updater type for uv.lock in release-please ([006a578](https://github.com/bakerkj/ha-vigil/commit/006a57879b96c61d0a65ea85780c8817f492d406))
* use toml updater type for uv.lock in release-please ([b5ea1bd](https://github.com/bakerkj/ha-vigil/commit/b5ea1bd60de64d296b97285ef48ddf236a67c7fe))


### Miscellaneous Chores

* **deps:** update anthropics/claude-code-action action to v1.0.183 ([e9e85bf](https://github.com/bakerkj/ha-vigil/commit/e9e85bffa88dfca0df0110cdeaa9b6bad1123389))
* **deps:** update anthropics/claude-code-action action to v1.0.183 ([dd60ce3](https://github.com/bakerkj/ha-vigil/commit/dd60ce39c45cea5cd6e9d7b3993aef59606f0ee0))
* **deps:** update dependency eslint to v10.8.0 ([50684e3](https://github.com/bakerkj/ha-vigil/commit/50684e3d2f586bacb0f66a779f35eb3b27e8b9d8))
* **deps:** update dependency eslint to v10.8.0 ([89217dc](https://github.com/bakerkj/ha-vigil/commit/89217dcd6ca8d616d8e207a593708bdc88beaaf0))
* drop the ruff dev dependency, matching the sibling ha-* repos ([540c08a](https://github.com/bakerkj/ha-vigil/commit/540c08ab4ef2c71e5f3806efa63caabd5d54c11b))
* drop the ruff dev dependency, matching the sibling ha-* repos ([4c69459](https://github.com/bakerkj/ha-vigil/commit/4c694598318033896ea30743feb3fc076aa724bf))
* track uv.lock version in release-please ([1cc21cc](https://github.com/bakerkj/ha-vigil/commit/1cc21cc7369bf9deb9e441dd8553ee042dffaf8c))
* track uv.lock version in release-please ([f8d3cc3](https://github.com/bakerkj/ha-vigil/commit/f8d3cc33d9ecee897ac600d7899cfaeba0f7c878))

## [0.0.3](https://github.com/bakerkj/ha-vigil/compare/v0.0.2...v0.0.3) (2026-07-24)


### Miscellaneous Chores

* **deps:** update astral-sh/setup-uv action to v9 ([542e653](https://github.com/bakerkj/ha-vigil/commit/542e65366e1b4dbb161fd248b95512e141b43625))
* **deps:** update astral-sh/setup-uv action to v9 ([5e2935d](https://github.com/bakerkj/ha-vigil/commit/5e2935dd70bed99ed9778c73441b51fb901b34a1))
* **deps:** update dependency ruff to ==0.16.* ([1eb429e](https://github.com/bakerkj/ha-vigil/commit/1eb429eb19255a717897af2a754de721de659b35))
* **deps:** update dependency ruff to ==0.16.* ([9fd1f39](https://github.com/bakerkj/ha-vigil/commit/9fd1f3999b55358319ffac85c914f94dc472a41b))
* **deps:** update github-actions ([d816677](https://github.com/bakerkj/ha-vigil/commit/d8166776f386be0ba0d00bcbfb9ef4696455440d))
* **deps:** update github-actions ([d13aeeb](https://github.com/bakerkj/ha-vigil/commit/d13aeebecd87b9415274212eeb9df890d9b2ec14))
* **deps:** update pre-commit hook astral-sh/ruff-pre-commit to v0.16.0 ([8c981c7](https://github.com/bakerkj/ha-vigil/commit/8c981c705dd7834c9399814179ac29c4925dfb3b))
* **deps:** update pre-commit hook astral-sh/ruff-pre-commit to v0.16.0 ([f8517f5](https://github.com/bakerkj/ha-vigil/commit/f8517f5b5004398d2f51b2201430b1051ff2992b))
* **deps:** update pre-commit hook rbubley/mirrors-prettier to v3.9.6 ([031c2c9](https://github.com/bakerkj/ha-vigil/commit/031c2c9f97c4701be7e2e947eeb8efcb7d288d6f))
* **deps:** update pre-commit hook rbubley/mirrors-prettier to v3.9.6 ([56dca6d](https://github.com/bakerkj/ha-vigil/commit/56dca6d24b819063a23b5601bb421e06d952cd11))
* **renovate:** drop redundant alternation in npm-in-pre-commit regex ([ba7790f](https://github.com/bakerkj/ha-vigil/commit/ba7790f50c855e67de75683799448982cb3d828b))
* **renovate:** drop redundant alternation in npm-in-pre-commit regex ([4e49960](https://github.com/bakerkj/ha-vigil/commit/4e499606ef2e4b0225e47c535c09b54618e2ba6f))


### Documentation

* call the dashboard surface a "card", not "panel", in comments ([6478e4d](https://github.com/bakerkj/ha-vigil/commit/6478e4de81859605f18ca5ad8a2ba173c38fb9ff))
* call the dashboard surface a "card", not "panel", in comments ([5ebcfc9](https://github.com/bakerkj/ha-vigil/commit/5ebcfc93b4886c0ba10808b50a8b90473636c950))


### Styles

* conform to ruff 0.16's expanded default rule set ([36129cc](https://github.com/bakerkj/ha-vigil/commit/36129cc02efa4ec736ee79c1ecb0097ce17b38e1))
* conform to ruff 0.16's expanded default rule set ([97c273d](https://github.com/bakerkj/ha-vigil/commit/97c273d0fbc211b4a743f33d5f7b867531f874ac))

## [0.0.2](https://github.com/bakerkj/ha-vigil/compare/v0.0.1...v0.0.2) (2026-07-21)


### Bug Fixes

* **ci:** pin claude-code-action to a known-working version ([8b4addc](https://github.com/bakerkj/ha-vigil/commit/8b4addc27af044a898478b5de56ac8a97b19ec4f))
* **ci:** pin claude-code-action to a known-working version ([e1a65b3](https://github.com/bakerkj/ha-vigil/commit/e1a65b3a95352654752e2b693bd216d79f901ee9))


### Miscellaneous Chores

* **deps:** update dependency @commitlint/config-conventional to v21 ([80a7ae0](https://github.com/bakerkj/ha-vigil/commit/80a7ae043410f2aef4c1a1c4600144d7b300932a))
* **deps:** update dependency @commitlint/config-conventional to v21 ([0011274](https://github.com/bakerkj/ha-vigil/commit/0011274537f4eb9e1592ec3f690ca1553407bfce))
* **deps:** update github-actions ([2fabb74](https://github.com/bakerkj/ha-vigil/commit/2fabb74efb3f68ee1fca52e8135bce981ab4fc4d))
* **deps:** update github-actions ([9ed0f90](https://github.com/bakerkj/ha-vigil/commit/9ed0f90a21c3fbba38eb74cf9d5322680054b065))
* **deps:** update github-actions ([7c8eac1](https://github.com/bakerkj/ha-vigil/commit/7c8eac12f5c7cd75da8c4b110ffcac6b9833df98))
* **deps:** update github-actions (major) ([af23142](https://github.com/bakerkj/ha-vigil/commit/af23142ab8eaa6f2e97801773692a99b0189c7db))
* **deps:** update pre-commit hooks ([154f182](https://github.com/bakerkj/ha-vigil/commit/154f182be5238e55b2276ed9296a8f3760a7fc1d))
* **deps:** update pre-commit hooks ([463b1bf](https://github.com/bakerkj/ha-vigil/commit/463b1bfc95504c54d58e40a80b929aa50dce48b5))
* initial import of the Vigil integration ([3f5509a](https://github.com/bakerkj/ha-vigil/commit/3f5509ac126d3fda3da7c341289562a94929fe94))
* **renovate:** align config with sibling repos ([e21e321](https://github.com/bakerkj/ha-vigil/commit/e21e3216e60afefe8c1957685d93a203caf4e20b))
* **renovate:** align config with sibling repos ([ca19cf9](https://github.com/bakerkj/ha-vigil/commit/ca19cf9d25bdbecdf822bb7408b759348c088818))
