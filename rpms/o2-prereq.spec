%global scl_name_prefix alice-
%global scl_name_base o2-full-deps

%global scl %{scl_name_prefix}%{scl_name_base}

# Optional but recommended: define nfsmountable
%global nfsmountable 1

%scl_package %scl
%global _scl_prefix /opt/alice

Summary: Package that installs %scl
Name: %scl_name
Version: 1
Release: 1%{?dist}
License: GPLv2+
Requires: make python-requests python-yaml pigz which git mysql-devel curl curl-devel bzip2 bzip2-devel unzip autoconf automake texinfo gettext gettext-devel libtool freetype freetype-devel libpng libpng-devel sqlite sqlite-devel ncurses-devel mesa-libGLU-devel libX11-devel libXpm-devel libXext-devel libXft-devel libXi-devel libxml2 libxml2-devel motif motif-devel kernel-devel pciutils-devel kmod-devel bison flex perl-ExtUtils-Embed environment-modules tk-devel
BuildRequires: scl-utils-build

%description
Metapackage which brings in all AliceO2 dependencies

%package runtime
Summary: Package that handles %scl Software Collection.
Requires: scl-utils

%description runtime
Package shipping essential scripts to work with %scl Software Collection.

%package build
Summary: Package shipping basic build configuration
Requires: scl-utils-build

%description build
Package shipping essential configuration macros to build %scl Software Collection.

%prep
%setup -c -T

%install
%scl_install

mkdir -p %{buildroot}%{_scl_prefix}
cat >> %{buildroot}%{_scl_scripts}/enable << EOF
export PATH="%{_bindir}:%{_sbindir}\${PATH:+:\${PATH}}"
export LD_LIBRARY_PATH="%{_libdir}\${LD_LIBRARY_PATH:+:\${LD_LIBRARY_PATH}}"
export MANPATH="%{_mandir}:\${MANPATH:-}"
export PKG_CONFIG_PATH="%{_libdir}/pkgconfig\${PKG_CONFIG_PATH:+:\${PKG_CONFIG_PATH}}"
EOF

# Install the generated man page
mkdir -p %{buildroot}%{_mandir}/man7/
#install -p -m 644 %{scl_name}.7 %{buildroot}%{_mandir}/man7/

%files

%files runtime -f filelist
%scl_files

%files build
%{_root_sysconfdir}/rpm/macros.%{scl}-config

%changelog
* Mon Nov 16 2020 Giulio Eulisse &lt;giulio.eulisse@cern.ch&gt; 1-1
- Initial package

