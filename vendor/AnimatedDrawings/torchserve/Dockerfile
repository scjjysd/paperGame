# syntax = docker/dockerfile:1.2

FROM continuumio/miniconda3:24.1.2-0

# 构建时可通过 --build-arg 传入宿主机代理；为空时保持直连。
ARG HTTP_PROXY
ARG HTTPS_PROXY
ARG http_proxy
ARG https_proxy
ENV HTTP_PROXY=${HTTP_PROXY} \
    HTTPS_PROXY=${HTTPS_PROXY} \
    http_proxy=${http_proxy} \
    https_proxy=${https_proxy}

# install os dependencies
RUN mkdir -p /usr/share/man/man1

# Debian Bullseye 已 EOL 归档，deb.debian.org/debian-security/pool/updates 下部分 .deb 被清理
# （如 vim/vim-runtime/libavahi-common3 出现 404），但 dists 索引仍在——"索引有、文件无"。
# 临时对策：丢弃 -security/-updates 套件，只保留 bullseye main（pool 完整）；
# 同时禁用 Valid-Until 检查，防止归档索引过期报错。
RUN sed -i -E '/bullseye-(security|updates)/d' /etc/apt/sources.list && \
    echo 'Acquire::Check-Valid-Until "false";' > /etc/apt/apt.conf.d/99no-check-valid-until

RUN apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get install --no-install-recommends -y \
    ca-certificates \
    curl \
    vim \
    sudo \
    default-jre \
    git \
    gcc \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN conda install python=3.8.13 -y


# install python dependencies
RUN pip install openmim
RUN pip install torch==2.0.0
RUN mim install mmcv-full==1.7.0
RUN pip install mmdet==2.27.0
RUN pip install torchserve

# bugfix for xtcocoapi, an mmpose dependency
RUN git clone https://github.com/jin-s13/xtcocoapi.git
WORKDIR xtcocoapi
RUN pip install -r requirements.txt
RUN python setup.py install
WORKDIR /
RUN pip install mmpose==0.29.0
RUN pip install torchvision==0.15.1  # solve torch version problem
RUN pip install numpy==1.24.4  #solve numpy version problem

# prep torchserve
RUN mkdir -p /home/torchserve/model-store
RUN wget https://github.com/facebookresearch/AnimatedDrawings/releases/download/v0.0.1/drawn_humanoid_detector.mar -P /home/torchserve/model-store/
RUN wget https://github.com/facebookresearch/AnimatedDrawings/releases/download/v0.0.1/drawn_humanoid_pose_estimator.mar -P /home/torchserve/model-store/
COPY config.properties /home/torchserve/config.properties

# starting command
CMD /opt/conda/bin/torchserve --start --disable-token-auth --ts-config /home/torchserve/config.properties && sleep infinity
