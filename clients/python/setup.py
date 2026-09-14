from setuptools import find_packages, setup

setup(
    name="dygine-pay",
    version="1.0.0",
    description="Client for Dygine Pay",
    packages=find_packages(),
    install_requires=["httpx>=0.27"],
    python_requires=">=3.10",
)
