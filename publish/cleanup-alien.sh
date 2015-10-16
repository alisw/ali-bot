#!/bin/bash -e
LIST=toClean.txt
[[ -e $LIST ]]
while read P; do
  PKGNAME=${P%% *}
  PKGVER=${P#* }
  [[ "$PKGNAME" != "$P" ]]
  [[ "$PKGVER" != "$P" ]]
  echo -n "Doing $PKGNAME $PKGVER: "
  while read ARCH; do
    echo -n "$ARCH..."
    alien -exec packman undefine \
                $PKGNAME $PKGVER -platform $ARCH -vo \
                -se ALICE::CERN::EOS > /dev/null 2>&1
    alien -exec ls /alice/packages/$PKGNAME/$PKGVER/$ARCH 2>&1 | grep -q "no such file"
  done < <(alien -exec ls /alice/packages/$PKGNAME/$PKGVER 2>&1 | grep -v "no such file")
  echo -n cleanup...
  alien -exec rmdir /alice/packages/$PKGNAME/$PKGVER > /dev/null 2>&1 || true
  alien -exec ls /alice/packages/$PKGNAME/$PKGVER 2>&1 | grep -q "no such file"
  echo done
done < <(cat $LIST)
echo All condemned packages removed
