# Copyright 2000, 2001, Compaq Computer Corporation

.PHONY: source clean

source: TypeSigVec.java

clean: 
	rm -rf *Vec.java

%Vec.java: ../util/_TYPE_Vec.j
	@sed s/_TYPE_/$*/g ../util/_TYPE_Vec.j | \
        sed s/_PKG_/javafe.tc/g               | \
        sed s/_COMPONENTPKG_/javafe.ast/g > $@

TypeSigVec.java: ../util/_TYPE_Vec.j
	@sed s/_TYPE_/TypeSig/g ../util/_TYPE_Vec.j | \
        sed s/_PKG_/javafe.tc/g               | \
        sed s/_COMPONENTPKG_/javafe.tc/g > $@
