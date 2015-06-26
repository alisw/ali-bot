#!/bin/sh -ex
git clone https://github.com/alisw/alibuild
git clone https://github.com/alisw/alidist
alibuild/aliBuild -d -a $ARCHITECTURE -j 16 build aliroot
