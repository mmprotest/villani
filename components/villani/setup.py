from __future__ import annotations

from packaging.tags import sys_tags
from setuptools import Distribution, setup
from wheel.bdist_wheel import bdist_wheel


class PlatformDistribution(Distribution):
    def has_ext_modules(self) -> bool:
        return True


class PlatformWheel(bdist_wheel):
    def finalize_options(self) -> None:
        super().finalize_options()
        self.root_is_pure = False

    def get_tag(self) -> tuple[str, str, str]:
        return "py3", "none", next(sys_tags()).platform


setup(distclass=PlatformDistribution, cmdclass={"bdist_wheel": PlatformWheel})
