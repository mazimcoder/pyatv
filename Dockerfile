

FROM amd64/ubuntu
#FROM arm32v7/ubuntu
MAINTAINER "Mustafa A. Azim"
#VOLUME ["app"]
EXPOSE 1000-60000/tcp
EXPOSE 1000-60000/udp
ENTRYPOINT [ "/bin/bash", "-l", "-c" ]

WORKDIR /app
ADD ./pyatv /app/
RUN apt-get -y update && \
    apt-get -y upgrade && \
#    apt-get -y install build-essential && \
    apt-get -y install python3 &&\
    apt-get -y install pip &&\
    python3 -m pip install --upgrade pip &&\
    python3 -m pip install zeroconf yarl wheel virtualenv urllib3 uritemplate typing-extensions tox toml srptools soupsieve six setuptools rsa requests pytest pytest-xdist pytest-runner pytest-forked pyparsing pycparser pyasn1 pyasn1-modules py protobuf pluggy platformdirs pip Pillow Pi packaging ordered-set netifaces mutagen multidict mediafile macholib iniconfig ifaddr idna httplib2 googleapis-common-protos google google-auth google-auth-httplib2 google-api-python-client google-api-core filelock execnet distlib deepdiff cryptography charset-normalizer cffi certifi cachetools bitarray beautifulsoup4 backports.entry-points-selectable attrs async-timeout altgraph aiohttp pillow miniaudio &&\
    python3 -m pip install https://github.com/pyinstaller/pyinstaller/archive/develop.tar.gz

RUN pyinstaller CompanionApi.py

ADD ./pyatv/ATVremotes/ATVsettings.json /app/dist/CompanionApi/ATVremotes/

WORKDIR /
RUN mkdir "app0"
WORKDIR /app
RUN cp -rp ./dist/CompanionApi /app0/

WORKDIR /
RUN rm -rf /app/

WORKDIR /app0/CompanionApi
CMD ["./CompanionApi"]
