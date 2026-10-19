// Bind retained native ESM exports to persistent reader references.
import {writeFileSync} from 'node:fs';
import {spawnSync} from 'node:child_process';
import {pathToFileURL} from 'node:url';
import {randomUUID} from 'node:crypto';
export class ImportBindings {
  constructor(ts) { this.ts=ts; this.bindings=new Map(); this.declared=new Set(); }
  prepare(source,file) {
    const ts=this.ts, f=ts.factory;
    const prefix=[...this.bindings].map(([name,b])=>`import { ${name} } from ${JSON.stringify(b.url)};`).join('\n');
    const hashbangEnd=source.startsWith('#!')?source.indexOf('\n'):-1;
    const hashbang=source.startsWith('#!')?source.slice(0,hashbangEnd<0?source.length:hashbangEnd+1):'';
    const hashbangLineBreak=hashbang&&!hashbang.endsWith('\n')?'\n':'';
    const bodySource=hashbang?source.slice(hashbang.length):source;
    const combined=hashbang+hashbangLineBreak+prefix+(prefix?'\n':'')+bodySource;
    const virtual=file+'.mts';
    const sf=ts.createSourceFile(virtual,combined,ts.ScriptTarget.Latest,true,ts.ScriptKind.JS);
    const opts={noLib:true,noResolve:true,target:ts.ScriptTarget.ESNext,module:ts.ModuleKind.ESNext};
    const host={getSourceFile:n=>n===virtual?sf:undefined,getDefaultLibFileName:()=>'',writeFile(){},getCurrentDirectory:()=>process.cwd(),getDirectories:()=>[],fileExists:n=>n===virtual,readFile:n=>n===virtual?combined:undefined,getCanonicalFileName:n=>n,useCaseSensitiveFileNames:()=>true,getNewLine:()=> '\n'};
    const checker=ts.createProgram([virtual],opts,host).getTypeChecker();
    const aliases=new Map(), imports=[], fresh=[];
    const prefixEnd=hashbang.length+hashbangLineBreak.length+prefix.length;
    function names(node){const c=node.importClause;if(!c)return [];return [...(c.name?[c.name]:[]),...(c.namedBindings?(ts.isNamespaceImport(c.namedBindings)?[c.namedBindings.name]:c.namedBindings.elements.map(x=>x.name)):[])];}
    for(const s of sf.statements) {
      if(!ts.isImportDeclaration(s))continue;
      const synthetic=s.end<=prefixEnd;
      if(!synthetic)imports.push(s.getText(sf));
      for(const name of names(s)){
        if(!synthetic){if(this.declared.has(name.text))throw new SyntaxError(`Identifier '${name.text}' has already been declared`);fresh.push(name.text);}
      }
    }
    let usesMetadata = false;
    const inspect = node => {
      if (ts.isMetaProperty(node) && node.keywordToken === ts.SyntaxKind.ImportKeyword
          && node.name.text === 'meta') usesMetadata = true;
      ts.forEachChild(node, inspect);
    };
    inspect(sf);
    if (usesMetadata && file.endsWith('.cjs')) {
      throw new SyntaxError('import.meta is unavailable in a CommonJS reader block');
    }
    if(!imports.length&&!this.bindings.size&&!usesMetadata){
      for(const symbol of checker.getSymbolsInScope(sf,ts.SymbolFlags.Value|ts.SymbolFlags.Alias))this.declared.add(symbol.name);
      return {code: source, metadataIdentifier: null};
    }
    const grammar=spawnSync(process.execPath,['--check','--input-type=module'],
      {input:combined,encoding:'utf8',timeout:5000});
    if(grammar.error || grammar.status!==0)throw new SyntaxError(grammar.stderr?.trim()||
      grammar.error?.message||'native module syntax check failed');
    const bridge=file+'.imports.mjs'; const bridgeUrl=pathToFileURL(bridge).href;
    if(imports.length)writeFileSync(bridge,imports.join('\n')+`\nexport { ${fresh.join(', ')} };\n`);
    const namespaces=new Map(),loads=[];
    for(const s of sf.statements){
      if(!ts.isImportDeclaration(s))continue;
      const synthetic=s.end<=prefixEnd;
      const url=synthetic?s.moduleSpecifier.text:bridgeUrl;
      if(!namespaces.has(url)){
        const id='__honua_import_'+randomUUID().replaceAll('-','');namespaces.set(url,id);
        loads.push(`const ${id} = await import(${JSON.stringify(url)});`);
      }
      for(const name of names(s))aliases.set(checker.getSymbolAtLocation(name),{namespace:namespaces.get(url),name:name.text});
    }
    const access=b=>f.createElementAccessExpression(f.createIdentifier(b.namespace),f.createStringLiteral(b.name));
    const alias=n=>ts.isIdentifier(n)?aliases.get(checker.getSymbolAtLocation(n)):undefined;
    const unparen=n=>ts.isParenthesizedExpression(n)?unparen(n.expression):n;
    const metadataIdentifier=usesMetadata?'__honua_meta_'+randomUUID().replaceAll('-',''):null;
    const transformed=ts.transform(sf,[ctx=>{
      const visit=n=>{
        if(ts.isImportDeclaration(n))return undefined;
        if(ts.isExportDeclaration(n)||ts.isExportAssignment(n))throw new SyntaxError('unsupported export declaration in reader context');
        if(metadataIdentifier&&ts.isMetaProperty(n)&&n.keywordToken===ts.SyntaxKind.ImportKeyword
            &&n.name.text==='meta')return f.createIdentifier(metadataIdentifier);
        if(ts.isCallExpression(n)&&!n.questionDotToken&&ts.isIdentifier(unparen(n.expression))&&unparen(n.expression).text==='eval'&&!checker.getSymbolAtLocation(unparen(n.expression)))throw new SyntaxError('direct eval with persistent imports is unsupported');
        if(ts.isShorthandPropertyAssignment(n)){
          const binding=aliases.get(checker.getShorthandAssignmentValueSymbol(n));
          if(binding){let value=access(binding);if(n.objectAssignmentInitializer)value=f.createBinaryExpression(value,f.createToken(ts.SyntaxKind.EqualsToken),ts.visitNode(n.objectAssignmentInitializer,visit));return f.createPropertyAssignment(n.name,value);}
        }
        if(ts.isCallExpression(n)&&alias(unparen(n.expression))){
          const callee=f.createParenthesizedExpression(f.createCommaListExpression([
            f.createNumericLiteral(0),access(alias(unparen(n.expression)))]));
          const args=ts.visitNodes(n.arguments,visit);
          return n.questionDotToken
            ? f.updateCallChain(n,callee,n.questionDotToken,n.typeArguments,args)
            : f.updateCallExpression(n,callee,n.typeArguments,args);
        }
        if(ts.isTaggedTemplateExpression(n)&&alias(unparen(n.tag))){
          return f.updateTaggedTemplateExpression(n,f.createParenthesizedExpression(f.createCommaListExpression([f.createNumericLiteral(0),access(alias(unparen(n.tag)))])),n.typeArguments,ts.visitNode(n.template,visit));
        }
        const binding=alias(n);if(binding)return access(binding);
        return ts.visitEachChild(n,visit,ctx);
      };return n=>ts.visitNode(n,visit);
    }]);
    let body=ts.createPrinter().printFile(transformed.transformed[0]);transformed.dispose();
    if(body.startsWith('#!')){
      const lineEnd=body.indexOf('\n');
      body=lineEnd<0?'':body.slice(lineEnd+1);
    }
    for(const name of fresh)this.bindings.set(name,{url:bridgeUrl});
    for(const symbol of checker.getSymbolsInScope(sf,ts.SymbolFlags.Value|ts.SymbolFlags.Alias))this.declared.add(symbol.name);
    return {code: loads.join('\n')+'\n'+body, metadataIdentifier};
  }
}
