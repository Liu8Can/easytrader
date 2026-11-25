# coding:utf8
from setuptools import setup


setup(
    name="easytrader",
    version="0.23.7",
    description="A utility for China Stock Trade",
    long_description=open("README.md", encoding="utf-8").read(),
    long_description_content_type="text/markdown",
    author="shidenggui",
    author_email="longlyshidenggui@gmail.com",
    license="BSD",
    url="https://github.com/shidenggui/easytrader",
    keywords="China stock trade",
    install_requires=[
        "requests>=2.31.0",
        "six>=1.16.0",
        "easyutils>=0.1.7",
        "flask>=3.0.0",
        "pywinauto>=0.6.8; sys_platform == 'win32'",
        "pywin32>=306; sys_platform == 'win32'",
        "pillow>=10.0.0",
        "pandas>=2.1.0",
        "pyperclip>=1.8.2",
    ],
    extras_require={
        "miniqmt": ["xtquant"],
        "mcp": ["mcp>=1.0.0"],
    },
    classifiers=[
        "Development Status :: 4 - Beta",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Programming Language :: Python :: 3.13",
        "License :: OSI Approved :: BSD License",
    ],
    packages=[
        "easytrader",
        "easytrader.config",
        "easytrader.utils",
        "easytrader.miniqmt",
    ],
    package_data={
        "": ["*.jar", "*.json"],
        "config": ["config/*.json"],
        "thirdlibrary": ["thirdlibrary/*.jar"],
    },
)
