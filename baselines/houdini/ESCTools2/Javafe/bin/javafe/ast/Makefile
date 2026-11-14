# Copyright 2000, 2001, Compaq Computer Corporation

#
# Dependence on astgen programs:
#

JAVAFE_ROOT ?= ../../../..
ASTGEN = 	${JAVAFE_ROOT}/Javafe/astgen/astgen
ASTFILELIST = 	${JAVAFE_ROOT}/Javafe/astgen/astfilelist

#
# Generating Java files:
#

TypeVec = ImportDeclVec.java TypeDeclVec.java TypeNameVec.java \
          TypeDeclElemVec.java FormalParaDeclVec.java StmtVec.java \
          CatchClauseVec.java \
          VarInitVec.java ExprVec.java IdentifierVec.java \
          ModifierPragmaVec.java LexicalPragmaVec.java \
          TypeModifierPragmaVec.java  MethodDeclVec.java FieldDeclVec.java

.PHONY: source
source: ASTNode.java typevecs

.PHONY: typevecs
typevecs: $(TypeVec) 

$(ASTFILELIST) $(ASTGEN):
	${MAKE} -C ${JAVAFE_ROOT}/Javafe/astgen

ASTNode.java TypeDeclElem.java tags: hierarchy.j $(ASTGEN) $(ASTFILELIST) \
		make_tags manual_tags insert_ensures manual_gettag_spec TypeDeclElem.j
	$(ASTGEN) < hierarchy.j
	@$(ASTFILELIST) < hierarchy.j | xargs grep -h '^public class' | \
	grep -v 'Visitor' | grep -v 'GeneratedTags' | grep -v 'class Type ' | \
	grep -v 'BinaryExpr' | grep -v 'UnaryExpr' | \
	grep -v 'LiteralExpr' | grep -v 'PrimitiveType' | \
	sed 's/.* class \([^ ]*\).*/\1/' | \
	./make_tags > tags
	@grep -v "#" manual_tags >> tags
	@cat tags | ./insert_ensures ASTNode > ASTNode.tmp
	@mv ASTNode.tmp ASTNode.java
	@cp TypeDeclElem.j TypeDeclElem.java
	@cat tags | ./insert_ensures TypeDeclElem > TypeDeclElem.tmp
	@mv TypeDeclElem.tmp TypeDeclElem.java

IntegerVec.java: ../util/_TYPE_Vec.j
	@sed s/_TYPE_/Integer/g ../util/_TYPE_Vec.j | \
		sed s/_PKG_/javafe.ast/g              | \
		sed s/_COMPONENTPKG_/java.lang/g > $@

%Vec.java: ../util/_TYPE_Vec.j
	@sed s/_TYPE_/$*/g ../util/_TYPE_Vec.j | \
		sed s/_PKG_/javafe.ast/g              | \
		sed s/_COMPONENTPKG_/javafe.ast/g > $@

#####

.PHONY: clean
clean: $(ASTFILELIST)
	@$(ASTFILELIST) < hierarchy.j | xargs rm -f 
	@rm -f ${TypeVec} ASTGEN* zASTGEN* *Vec.spec
	@rm -f tags ASTNode.before ASTNode.after ASTNode.tmp ASTNode.java
	@rm -rf *.class ${CLASSDIRECTORY}/javafe/ast/*.class
	@rm -f TypeDeclElem.java TypeDeclElem.before TypeDeclElem.after
	@rm -f TypeDeclElem.tmp
