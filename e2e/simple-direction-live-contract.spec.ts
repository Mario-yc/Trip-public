import { expect, test } from "@playwright/test";
import { readFile } from "node:fs/promises";
import path from "node:path";
import * as ts from "../frontend/node_modules/typescript/lib/typescript.js";

test("live clarification path invokes the imported detour selector in its active detour branch", async () => {
  const sourcePath = path.resolve(
    "e2e",
    "simple-direction-user-journey.spec.ts",
  );
  const sourceText = await readFile(sourcePath, "utf8");
  const sourceFile = ts.createSourceFile(
    sourcePath,
    sourceText,
    ts.ScriptTarget.Latest,
    true,
    ts.ScriptKind.TS,
  );

  const selectorImport = sourceFile.statements.find(
    (statement): statement is ts.ImportDeclaration =>
      ts.isImportDeclaration(statement) &&
      ts.isStringLiteral(statement.moduleSpecifier) &&
      statement.moduleSpecifier.text === "./support/detour-option-selector",
  );
  expect(selectorImport).toBeTruthy();
  const namedImports = selectorImport?.importClause?.namedBindings;
  expect(
    namedImports &&
      ts.isNamedImports(namedImports) &&
      namedImports.elements.some(
        (element) => element.name.text === "selectStrictDetourOption",
      ),
  ).toBe(true);

  const clarificationFunction = sourceFile.statements.find(
    (statement): statement is ts.FunctionDeclaration =>
      ts.isFunctionDeclaration(statement) &&
      statement.name?.text === "completeClarificationBatches",
  );
  expect(clarificationFunction?.body).toBeTruthy();

  function collect<T extends ts.Node>(
    root: ts.Node,
    predicate: (node: ts.Node) => node is T,
  ): T[] {
    const matches: T[] = [];
    function visit(node: ts.Node) {
      if (predicate(node)) matches.push(node);
      ts.forEachChild(node, visit);
    }
    visit(root);
    return matches;
  }
  const body = clarificationFunction!.body!;
  const detourBranches = collect(
    body,
    (node): node is ts.IfStatement =>
      ts.isIfStatement(node) &&
      node.expression.getText(sourceFile) ===
        'dimensionId === "route_decision.detour_tolerance"',
  );
  const selectorBranches = detourBranches.filter(
    (branch) =>
      collect(
        branch.thenStatement,
        (node): node is ts.CallExpression =>
          ts.isCallExpression(node) &&
          ts.isIdentifier(node.expression) &&
          node.expression.text === "selectStrictDetourOption",
      ).length,
  );
  expect(selectorBranches).toHaveLength(1);
  const selectorBranch = selectorBranches[0].thenStatement;

  const selectedDeclarations = collect(
    selectorBranch,
    (node): node is ts.VariableDeclaration =>
      ts.isVariableDeclaration(node) &&
      ts.isIdentifier(node.name) &&
      node.name.text === "selected" &&
      Boolean(
        node.initializer &&
        ts.isCallExpression(node.initializer) &&
        ts.isIdentifier(node.initializer.expression) &&
        node.initializer.expression.text === "selectStrictDetourOption" &&
        node.initializer.arguments.length === 1 &&
        ts.isIdentifier(node.initializer.arguments[0]) &&
        node.initializer.arguments[0].text === "question",
      ),
  );
  expect(selectedDeclarations).toHaveLength(1);

  const branchAssignments = collect(
    selectorBranch,
    (node): node is ts.BinaryExpression =>
      ts.isBinaryExpression(node) &&
      node.operatorToken.kind === ts.SyntaxKind.EqualsToken,
  );
  expect(
    branchAssignments.some(
      (assignment) =>
        assignment.left.getText(sourceFile) === "option" &&
        assignment.right.getText(sourceFile).includes("selected.optionId"),
    ),
  ).toBe(true);

  expect(
    branchAssignments.some(
      (assignment) =>
        assignment.left.getText(sourceFile) === "boundedSemanticValue" &&
        assignment.right.getText(sourceFile) === "selected.semanticValue",
    ),
  ).toBe(true);

  const optionAssignments = collect(
    body,
    (node): node is ts.BinaryExpression =>
      ts.isBinaryExpression(node) &&
      node.operatorToken.kind === ts.SyntaxKind.EqualsToken &&
      node.left.getText(sourceFile) === "option",
  );
  const semanticAssignments = collect(
    body,
    (node): node is ts.BinaryExpression =>
      ts.isBinaryExpression(node) &&
      node.operatorToken.kind === ts.SyntaxKind.EqualsToken &&
      node.left.getText(sourceFile) === "boundedSemanticValue",
  );
  expect(optionAssignments).toHaveLength(2);
  expect(semanticAssignments).toHaveLength(2);

  const optionIdentityDeclarations = collect(
    body,
    (node): node is ts.VariableDeclaration =>
      ts.isVariableDeclaration(node) &&
      ts.isIdentifier(node.name) &&
      node.name.text === "optionId" &&
      node.initializer?.getText(sourceFile) === 'String(option.id || "")',
  );
  expect(optionIdentityDeclarations).toHaveLength(1);

  const artifactPushes = collect(
    body,
    (node): node is ts.CallExpression =>
      ts.isCallExpression(node) &&
      ts.isPropertyAccessExpression(node.expression) &&
      node.expression.expression.getText(sourceFile) === "selections" &&
      node.expression.name.text === "push" &&
      node.arguments.length === 1 &&
      ts.isObjectLiteralExpression(node.arguments[0]),
  );
  expect(artifactPushes).toHaveLength(1);
  const artifactText = artifactPushes[0].arguments[0].getText(sourceFile);
  expect(artifactText).toContain("optionId");
  expect(artifactText).toContain("semanticValue: boundedSemanticValue");
  expect(artifactText).toContain('submissionMode: "persisted_option"');

  const optionIdentityReads = collect(
    body,
    (node): node is ts.CallExpression =>
      ts.isCallExpression(node) &&
      ts.isPropertyAccessExpression(node.expression) &&
      node.expression.name.text === "getAttribute" &&
      ts.isCallExpression(node.expression.expression) &&
      ts.isPropertyAccessExpression(node.expression.expression.expression) &&
      node.expression.expression.expression.name.text === "nth" &&
      node.expression.expression.expression.expression.getText(sourceFile) ===
        "serverOptionRadios" &&
      node.expression.expression.arguments.length === 1 &&
      node.expression.expression.arguments[0].getText(sourceFile) === "index" &&
      node.arguments.length === 1 &&
      node.arguments[0].getText(sourceFile) === '"data-option-id"',
  );
  expect(optionIdentityReads).toHaveLength(1);
  const exactOptionMatches = collect(
    body,
    (node): node is ts.BinaryExpression =>
      ts.isBinaryExpression(node) &&
      node.operatorToken.kind === ts.SyntaxKind.EqualsEqualsEqualsToken &&
      node.right.getText(sourceFile) === "optionId" &&
      collect(
        node.left,
        (candidate): candidate is ts.CallExpression =>
          candidate === optionIdentityReads[0],
      ).length === 1,
  );
  expect(exactOptionMatches).toHaveLength(1);

  const bodyText = body.getText(sourceFile);
  expect(bodyText).toContain(
    "await serverOptionRadios.nth(matchingRadioIndex).check()",
  );

  expect(bodyText).not.toContain("maxGeneralizedCostDelta");
  expect(bodyText).not.toContain("maxDetourRatio");
});

test("live journey validates exact clarification dimension identities without arrival-order coupling", async () => {
  const sourcePath = path.resolve(
    "e2e",
    "simple-direction-user-journey.spec.ts",
  );
  const sourceText = await readFile(sourcePath, "utf8");
  const sourceFile = ts.createSourceFile(
    sourcePath,
    sourceText,
    ts.ScriptTarget.Latest,
    true,
    ts.ScriptKind.TS,
  );

  const contractImport = sourceFile.statements.find(
    (statement): statement is ts.ImportDeclaration =>
      ts.isImportDeclaration(statement) &&
      ts.isStringLiteral(statement.moduleSpecifier) &&
      statement.moduleSpecifier.text ===
        "./support/clarification-dimension-contract",
  );
  const namedImports = contractImport?.importClause?.namedBindings;
  expect(
    namedImports &&
      ts.isNamedImports(namedImports) &&
      namedImports.elements.some(
        (element) =>
          element.name.text === "validateClarificationDimensionIdentitySet",
      ),
  ).toBe(true);

  const calls: ts.CallExpression[] = [];
  function visit(node: ts.Node) {
    if (
      ts.isCallExpression(node) &&
      ts.isIdentifier(node.expression) &&
      node.expression.text === "validateClarificationDimensionIdentitySet"
    ) {
      calls.push(node);
    }
    ts.forEachChild(node, visit);
  }
  visit(sourceFile);
  expect(calls).toHaveLength(1);
  expect(calls[0].arguments).toHaveLength(2);
  expect(calls[0].arguments[0].getText(sourceFile)).toBe(
    "clarificationBatchEvidence.dimensions",
  );
  expect(calls[0].arguments[1].getText(sourceFile)).toBe(
    "EXPECTED_CLARIFICATION_DIMENSIONS",
  );
  expect(sourceText).not.toContain(
    "expect(clarificationBatchEvidence.dimensions).toEqual",
  );
});

test("live journey binds the rendered clarification card to the current server checkpoint before submit", async () => {
  const sourcePath = path.resolve(
    "e2e",
    "simple-direction-user-journey.spec.ts",
  );
  const sourceText = await readFile(sourcePath, "utf8");
  const sourceFile = ts.createSourceFile(
    sourcePath,
    sourceText,
    ts.ScriptTarget.Latest,
    true,
    ts.ScriptKind.TS,
  );
  const clarificationFunction = sourceFile.statements.find(
    (statement): statement is ts.FunctionDeclaration =>
      ts.isFunctionDeclaration(statement) &&
      statement.name?.text === "completeClarificationBatches",
  );
  expect(clarificationFunction?.body).toBeTruthy();
  const bodyText = clarificationFunction!.body!.getText(sourceFile);
  expect(bodyText).toContain('card.getAttribute("data-checkpoint-id")');
  expect(bodyText).toContain('card.getAttribute("data-source-turn-id")');
  expect(bodyText).toContain("expect(domCheckpointId).toBe(checkpointId)");
  expect(bodyText).toContain(
    "expect(sourceTurnId).toBe(sourceAssistantTurnId)",
  );

  const checkpointRead = bodyText.indexOf(
    'card.getAttribute("data-checkpoint-id")',
  );
  const submitClick = bodyText.indexOf("await submit.click()");
  expect(checkpointRead).toBeGreaterThanOrEqual(0);
  expect(submitClick).toBeGreaterThan(checkpointRead);
});

test("live journey consumes only bounded server-signed continuations before requiring proposal A", async () => {
  const sourcePath = path.resolve(
    "e2e",
    "simple-direction-user-journey.spec.ts",
  );
  const sourceText = await readFile(sourcePath, "utf8");
  const sourceFile = ts.createSourceFile(
    sourcePath,
    sourceText,
    ts.ScriptTarget.Latest,
    true,
    ts.ScriptKind.TS,
  );

  const continuationFunction = sourceFile.statements.find(
    (statement): statement is ts.FunctionDeclaration =>
      ts.isFunctionDeclaration(statement) &&
      statement.name?.text === "advanceBlockedInitialDirections",
  );
  expect(continuationFunction?.body).toBeTruthy();
  const continuationText = continuationFunction!.body!.getText(sourceFile);
  expect(continuationText).toContain(
    'data-choice-action="continue_plan_expansion"',
  );
  expect(continuationText).toContain("MAX_PRE_ADOPTION_CONTINUATIONS");
  expect(continuationText).toContain("sourceAssistantTurnId");
  expect(continuationText).toContain("requestContractFingerprint");
  expect(continuationText).toContain("planningSelectionRootTurnId");
  expect(continuationText).toContain("rootPortfolioId");

  const journey = sourceFile.statements.find(
    (statement): statement is ts.ExpressionStatement =>
      ts.isExpressionStatement(statement) &&
      ts.isCallExpression(statement.expression) &&
      statement.expression.expression.getText(sourceFile) === "test",
  );
  expect(journey).toBeTruthy();
  const journeyText = journey!.getText(sourceFile);
  const continuationCall = journeyText.indexOf(
    "advanceBlockedInitialDirections(",
  );
  const readyAssertion = journeyText.indexOf("cards.count()", continuationCall);
  expect(continuationCall).toBeGreaterThan(0);
  expect(readyAssertion).toBeGreaterThan(continuationCall);
});
